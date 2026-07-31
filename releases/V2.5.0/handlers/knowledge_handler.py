"""
知识库问答处理器
搜索 IMA 知识库并格式化搜索结果回复

核心策略：
  IMA search_knowledge API 是词级匹配（非语义），长句子搜不到结果。
  因此需要：
  1. 从用户消息中提取核心关键词（2-6字）
  2. 对每个关键词分别搜索
  3. 合并去重所有结果
"""

import re
from utils.logger import get_logger

logger = get_logger("knowledge-handler")

# 搜索结果中提取摘要用的正则
_HIGHLIGHT_PATTERN = re.compile(r"<[^>]+>", re.IGNORECASE)

# 文件类型映射
MEDIA_TYPE_ICONS = {
    1: "📄",   # PDF
    3: "📄",   # Word
    4: "📊",   # PPT
    5: "📊",   # Excel
    7: "🖼️",   # 图片
    9: "🖼️",   # 图片
    11: "📝",  # 笔记
    12: "📁",  # 文件夹
    14: "🧠",  # 思维导图
    99: "📁",  # 文件夹
}

# IMA 搜索不支持的/无意义词（停用词）
_STOP_WORDS = set(
    "的 了 在 是 我 有 和 就 不 人 都 一 个 上 也 很 到 说 要 去 你 会 着 没有 看 好 "
    "自己 这 他 她 它 们 么 什么 那 这个 那个 哪 哪里 怎么 如何 为什么 能 不能 "
    "可以 可不可以 应该 需要 已经 还 没 吧 呢 啊 哦 嗯 哈 呀 "
    "请 帮 帮我 让 给 把 被 从 对 向 以 用 为 与 及 但 而 或 "
    "之 其 中 后 前 里 外 些 多 少 大 小 新 旧 最 更 比 等等 "
    "知道 想知道 想要 试试 试试看 告诉 查一下 搜一下 找一下 "
    "一下 下 一 哪个 哪些 怎样 什么样 什么意思 怎么回事"
    .split()
)

# IT 运维领域复合词表（切词时整体保留，不再拆分）
# 优先级高于滑动窗口切词，先整词匹配再切剩余部分
DOMAIN_TERMS = {
    # U9C / ERP 相关
    "U9", "U8C", "U9C", "ERP", "CRM",
    "料号", "料号修改", "物料编码", "物料号", "存货档案",
    "财务凭证", "记账凭证", "采购订单", "销售订单", "发货单",
    "入库单", "出库单", "盘点单", "调拨单", "对账单",
    "应收账款", "应付账款", "账期", "结算", "核销",
    "审批流", "审批权限", "工作流", "流程审批",
    "供应商", "客户档案", "往来账", "总账", "明细账",
    # 系统账号/权限
    "域账号", "AD域", "域控", "账号密码", "密码重置",
    "VPN", "远程桌面", "远程访问", "远程连接",
    "账号权限", "权限申请", "账号申请", "账号开通", "账号注销",
    # 网络/硬件
    "IP地址", "DNS", "网关", "子网掩码", "MAC地址",
    "交换机", "路由器", "防火墙", "无线网", "有线网",
    "网络打印", "打印机", "驱动安装", "共享打印",
    "电脑蓝屏", "系统崩溃", "系统报错", "蓝屏", "死机",
    # 软件/工具
    "帆软", "FineReport", "报表", "数据报表",
    "钉钉", "企业微信", "飞书", "办公软件",
    "浏览器", "Chrome", "Edge", "Internet Explorer",
    "杀毒软件", "安全软件",
    # 常见问题类型
    "系统卡顿", "系统慢", "登录失败", "无法登录", "忘记密码",
    "数据导入", "数据导出", "批量导入", "模板下载",
    "版本升级", "系统更新", "补丁", "安装包",
    # NAS / 服务器
    "NAS", "文件服务器", "共享文件夹", "网络存储",
    "服务器", "虚拟机", "云服务器",
}


def strip_html(text: str) -> str:
    """去除 HTML 标签，提取纯文本"""
    if not text:
        return ""
    return _HIGHLIGHT_PATTERN.sub("", text).strip()


def should_search_knowledge(text: str, trigger_keywords: list = None, expert_triggers: list = None) -> bool:
    """
    判断消息是否应该触发知识库搜索
    :param text: 用户消息文本
    :param trigger_keywords: 兼容触发关键词列表，如 ["搜", "查", "找"]
    :param expert_triggers: 召唤专家触发词列表，如 [{"phrase": "召唤运维专家", ...}]
    :return: 是否触发搜索
    """
    # 先检查专家触发词（以短语开头）
    if expert_triggers:
        for trigger in expert_triggers:
            if text.strip().startswith(trigger["phrase"]):
                return True
    # 再检查兼容关键词（包含即触发）
    if trigger_keywords:
        for kw in trigger_keywords:
            if kw in text:
                return True
    return False


def match_expert_trigger(text: str, expert_triggers: list) -> dict:
    """
    匹配专家触发词，返回匹配的触发配置或 None
    """
    if not expert_triggers:
        return None
    for trigger in expert_triggers:
        if text.strip().startswith(trigger["phrase"]):
            return trigger
    return None


def extract_query(text: str, bot_name: str = "", trigger_keywords: list = None, expert_triggers: list = None) -> str:
    """
    从消息中提取搜索查询关键词
    去掉 @机器人、专家触发词、搜/查/找 等干扰信息，以及开头的标点/请求前缀
    """
    query = text.strip()

    # 去掉 @机器人
    if bot_name:
        query = re.sub(rf"@\s*{re.escape(bot_name)}", "", query, flags=re.IGNORECASE)

    # 构建前缀列表
    prefixes = []

    # 专家触发词前缀（优先匹配，因为更长更精确）
    if expert_triggers:
        for trigger in expert_triggers:
            prefixes.append(trigger["phrase"])

    # 兼容触发关键词 + 常见复合前缀
    common_prefixes = ["搜索", "搜一下", "查一下", "查查", "找一下", "找找",
                       "帮我搜", "帮我查", "帮我找", "知识库里有没有",
                       "知识库搜索", "搜知识库", "查知识库"]
    if trigger_keywords:
        common_prefixes = list(dict.fromkeys(trigger_keywords + common_prefixes))
    prefixes.extend(common_prefixes)

    # 按长度从长到短排序，优先去掉更长的前缀
    prefixes = list(dict.fromkeys(prefixes))  # 去重
    prefixes.sort(key=len, reverse=True)
    for prefix in prefixes:
        if query.startswith(prefix):
            query = query[len(prefix):].strip()
            break

    # 清理开头的标点符号和常见请求前缀
    query = re.sub(r"^[，,、.。:：;；!?！？\s]+", "", query)
    request_prefixes = ["告诉我", "请问", "我想知道", "帮我看看", "帮我查下",
                        "帮我搜下", "帮我找下", "帮我查一下", "帮我搜一下",
                        "帮我找一下", "请问一下", "问一下", "咨询一下",
                        "给我说说", "给我讲讲", "给我查一下", "给我搜一下"]
    for rp in sorted(request_prefixes, key=len, reverse=True):
        if query.startswith(rp):
            query = query[len(rp):].strip()
            break
    query = re.sub(r"^[，,、.。:：;；!?！？\s]+", "", query)

    return query


def extract_keywords(text: str) -> list:
    """
    从用户消息中提取搜索关键词（适配 IMA 词级匹配引擎）

    IMA search_knowledge 是词级精确匹配：
      - "U9" → 67条
      - "U9操作" → 3条
      - "U9怎么用啊" → 0条

    策略：
      1. 优先：整词匹配 DOMAIN_TERMS 中的领域复合词（如"料号修改"、"账号权限"）
      2. 保留英文/数字连续串（如 "U9"、"U8C"、"ERP"）
      3. 剩余中文按 2-4 字滑动窗口切词（优先取2字）
      4. 过滤停用词，去重，优先短词（IMA 短词命中率更高）

    :param text: 已清理前缀的用户消息（如 "U9料号怎么修改"）
    :return: 关键词列表，按优先级排序
    """
    if not text:
        return []

    keywords = []
    seen = set()

    # ── 步骤1：整词匹配领域词表（最高优先级） ──
    # 按词长从长到短匹配，避免"料号修改"被切成"料号"+"修改"
    sorted_terms = sorted(DOMAIN_TERMS, key=len, reverse=True)
    remaining_text = text
    for term in sorted_terms:
        if term in remaining_text:
            term_upper = term.upper()
            if term_upper not in seen:
                keywords.append(term)
                seen.add(term_upper)
            # 从文本中去掉已匹配的领域词，防止后续切词重复
            remaining_text = remaining_text.replace(term, " ")

    # ── 步骤2：提取英文+数字连续串（如 U9, ERP, CRM, v2.0） ──
    en_matches = re.findall(r'[A-Za-z][A-Za-z0-9_.\-]*', remaining_text)
    for m in en_matches:
        word = m.strip("_.- ")
        if word and word.upper() not in seen and len(word) >= 1:
            keywords.append(word.upper())
            seen.add(word.upper())

    # ── 步骤3：剩余中文滑动窗口切词（2字 > 3字 > 4字） ──
    chinese_text = re.sub(r'[A-Za-z][A-Za-z0-9_.\-]*', '', remaining_text)
    chinese_text = re.sub(r'[^\u4e00-\u9fff]', '', chinese_text)

    cn_words = []
    for length in [2, 3, 4]:
        for i in range(0, len(chinese_text) - length + 1, length):
            word = chinese_text[i:i + length]
            if word not in _STOP_WORDS and word not in seen:
                cn_words.append(word)
                seen.add(word)

    # ── 步骤4：合并：领域词（高优先）+ 英文词 + 中文2字词 + 中文3字词 ──
    keywords.extend(cn_words)

    if not keywords:
        # 兜底：用整个文本作为 query
        keywords.append(text.strip())

    logger.debug(f"extract_keywords: '{text}' -> {keywords}")
    return keywords


def search_and_format(client, query: str, max_results: int = 5, default_kbs: list = None) -> str:
    """
    搜索知识库并格式化为 Markdown 回复

    搜索策略（适配 IMA 词级匹配）：
      1. 先用完整 query 搜索（针对用户直接输入短关键词的场景）
      2. 如果无结果，拆分关键词逐个搜索
      3. 合并去重所有结果

    :param client: IMAClient 实例
    :param query: 搜索关键词
    :param max_results: 最大返回结果数
    :param default_kbs: 默认搜索的知识库列表
    :return: Markdown 格式的回复文本
    """
    if not query:
        return "请告诉我你想搜索什么内容哦～ 🦊"

    logger.info(f"搜索知识库: query='{query}', default_kbs={len(default_kbs) if default_kbs else 'all'}")

    # 策略1：先用完整 query 搜索
    if default_kbs:
        results = client.search_knowledge_bases(query, default_kbs)
    else:
        results = client.search_all_knowledge_bases(query)

    # 策略2：如果完整 query 搜不到，拆分关键词逐个搜索
    if not results and len(query) > 2:
        kw_list = extract_keywords(query)
        logger.info(f"完整query无结果，拆分关键词搜索: {kw_list}")

        seen_titles = set()  # 按标题去重
        for kw in kw_list:
            if len(results) >= max_results * 2:
                break  # 已经够了，不用继续
            kw_results = client.search_knowledge_bases(kw, default_kbs) if default_kbs else client.search_all_knowledge_bases(kw)
            for item in kw_results:
                title = item.get("title", "")
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    # 标注匹配到的关键词
                    item["matched_keyword"] = kw
                    results.append(item)
            logger.info(f"  关键词 '{kw}' 搜到 {len(kw_results)} 条 (累计 {len(results)})")

    if not results:
        return f"在知识库里没有找到与「**{query}**」相关的内容 (´・ω・`)\n\n可能的原因：\n- 关键词太具体，试试换个说法\n- 该内容尚未录入知识库"

    # 截取结果
    results = results[:max_results]

    # 格式化为 Markdown
    lines = [f"🔍 在知识库中搜索「**{query}**」的结果：\n"]

    for i, item in enumerate(results, 1):
        title = item.get("title", "未知标题")
        source_kb = item.get("source_kb", "未知知识库")
        media_type = item.get("media_type", 0)

        # 提取高亮摘要
        highlight = strip_html(item.get("highlight", ""))
        if not highlight:
            highlight = strip_html(item.get("highlight_content", ""))
        if not highlight:
            highlight = strip_html(item.get("description", ""))
        if not highlight:
            highlight = "(暂无摘要)"

        # 文件类型图标
        icon = MEDIA_TYPE_ICONS.get(media_type, "📄")

        # 如果是通过关键词拆分搜到的，标注匹配词
        matched_kw = item.get("matched_keyword", "")
        kw_tag = f" (匹配: {matched_kw})" if matched_kw else ""

        lines.append(f"{i}. {icon} **{title}**{kw_tag}")
        lines.append(f"   📚 来源: {source_kb}")
        if highlight and highlight != "(暂无摘要)":
            if len(highlight) > 300:
                highlight = highlight[:300] + "..."
            lines.append(f"   > {highlight}")
        lines.append("")

    return "\n".join(lines)
