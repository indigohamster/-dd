# ============================================================
# 工单系统功能测试
# 验证：创建/查询/分配/状态流转/关闭/统计/反馈闭环 全流程
# ============================================================

import sys
import os

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.ticket import TicketManager, TicketParser, TicketFormatter
from modules.ticket.ticket_parser import TicketIntent
from modules.ticket.ticket_manager import (
    STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS,
    STATUS_RESOLVED, STATUS_CLOSED, STATUS_LABELS,
)

TEST_DB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "tickets_test.db"
)

def run_legacy_suite():
    print("=" * 60)
    print("  📋 工单系统功能测试")
    print("=" * 60)
    passed = 0
    failed = 0

    def check(name, condition):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {name}")
        else:
            failed += 1
            print(f"  ❌ {name}")

    # 清理测试数据库
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    mgr = TicketManager(TEST_DB)

    # ---------------------------------------------------------------
    # 1. 创建工单
    # ---------------------------------------------------------------
    print("\n── 1. 创建工单 ──")

    t1 = mgr.create_ticket(
        conv_id="conv_001",
        conv_title="IT运维群",
        creator_id="user_001",
        creator_nick="张三",
        title="打印机无法打印",
        description="HP LaserJet 打印任务卡住",
        category="HARDWARE",
    )
    check("创建成功", t1 is not None)
    check("编号格式正确", t1["ticket_id"].startswith("TKT-"))
    check("状态为 open", t1["status"] == STATUS_OPEN)
    check("分类为 HARDWARE", t1["category"] == "HARDWARE")
    print(f"  工单编号: {t1['ticket_id']}")

    t2 = mgr.create_ticket(
        conv_id="conv_002",
        conv_title="财务部群",
        creator_id="user_002",
        creator_nick="李四",
        title="ERP 凭证无法过账",
        description="销售凭证 PE2026-001 过账报错",
        category="业务系统",
        priority="high",
    )
    check("创建第二个工单", t2 is not None)
    check("不同编号", t2["ticket_id"] != t1["ticket_id"])

    t3 = mgr.create_ticket(
        conv_id="conv_003",
        conv_title="IT运维群",
        creator_id="user_003",
        creator_nick="王五",
        title="VPN 连接不上",
        description="在家办公时 VPN 总是断开",
        category="NETWORK",
    )
    check("创建第三个工单", t3 is not None)

    # ---------------------------------------------------------------
    # 2. 查询工单
    # ---------------------------------------------------------------
    print("\n── 2. 查询工单 ──")

    ticket = mgr.get_ticket(t1["ticket_id"])
    check("按编号查询", ticket is not None)
    check("查询结果正确", ticket["creator_nick"] == "张三")

    missing = mgr.get_ticket("TKT-99999999-999")
    check("查询不存在的工单返回 None", missing is None)

    user_tickets = mgr.list_tickets_by_user("user_001")
    check("按用户查询", len(user_tickets) == 1)

    # ---------------------------------------------------------------
    # 3. 状态流转
    # ---------------------------------------------------------------
    print("\n── 3. 状态流转 ──")

    # 分配
    assigned = mgr.assign_ticket(
        t1["ticket_id"], "admin_001", "管理员",
        "admin_001", "管理员",
    )
    check("分配工单", assigned is not None)
    check("状态变为 assigned", assigned["status"] == STATUS_ASSIGNED)
    check("负责人已设置", assigned["assignee_nick"] == "管理员")

    # 开始处理
    started = mgr.update_status(
        t1["ticket_id"], STATUS_IN_PROGRESS,
        "admin_001", "管理员", "开始处理",
    )
    check("开始处理", started is not None)
    check("状态变为 in_progress", started["status"] == STATUS_IN_PROGRESS)

    # 解决
    resolved = mgr.resolve_ticket(
        t1["ticket_id"], "重启打印服务后恢复",
        "admin_001", "管理员",
    )
    check("解决工单", resolved is not None)
    check("状态变为 resolved", resolved["status"] == STATUS_RESOLVED)
    check("解决方案已记录", "重启打印服务" in resolved["resolution"])

    # 用户确认后关闭
    closed = mgr.confirm_resolution(t1["ticket_id"], "user_001", "张三")
    check("关闭工单", closed is not None)
    check("状态变为 closed", closed["status"] == STATUS_CLOSED)

    # ---------------------------------------------------------------
    # 4. 操作日志
    # ---------------------------------------------------------------
    print("\n── 4. 操作日志 ──")

    logs = mgr.get_ticket_logs(t1["ticket_id"])
    actions = [log["action"] for log in logs]
    check("操作日志记录", len(logs) >= 5)
    check("包含 create", "create" in actions)
    check("包含 confirm", "confirm" in actions)
    check("包含 assign", "assign" in actions)
    print(f"  共 {len(logs)} 条操作记录: {actions}")

    # ---------------------------------------------------------------
    # 5. 统计
    # ---------------------------------------------------------------
    print("\n── 5. 统计数据 ──")

    stats = mgr.get_stats()
    check("总工单数正确", stats["total"] == 3)
    check("待处理数正确", stats["open"] == 2)  # t2(high), t3 未处理
    check("已解决数正确", stats["resolved"] == 1)  # t1 已关闭

    # ---------------------------------------------------------------
    # 6. 分类
    # ---------------------------------------------------------------
    print("\n── 6. 自动分类 ──")

    check("业务系统问题分类", TicketManager.classify("ERP凭证无法过账") == "业务系统问题")
    check("设备问题分类", TicketManager.classify("打印机坏了") == "设备问题")
    check("软件问题分类", TicketManager.classify("office打不开") == "软件问题")
    check("CRM问题分类", TicketManager.classify("CRM客户不能查") == "CRM问题")
    check("其他分类", TicketManager.classify("今天天气不错") == "其他")
    check("网络fallback分类", TicketManager.classify("VPN连不上") == "设备问题")

    # KB 分类测试
    fake_kb_results = [
        {"title": "凭证过账报错", "source_kb_id": "kb_erp_example", "source_kb": "ERP问题知识库"},
        {"title": "客户端无法启动", "source_kb_id": "kb_device_example", "source_kb": "日常硬件维护"},
    ]
    check("KB ERP 分类", TicketManager.classify_by_kb(fake_kb_results[:1]) == "业务系统问题")
    check("KB 设备分类", TicketManager.classify_by_kb(fake_kb_results[1:]) == "设备问题")
    check("KB 空结果", TicketManager.classify_by_kb([]) == "其他")

    # ---------------------------------------------------------------
    # 7. 意图解析
    # ---------------------------------------------------------------
    print("\n── 7. 意图解析 ──")

    p = TicketParser.parse("报修 打印机故障")
    check("创建意图", p.intent == TicketIntent.CREATE)

    p = TicketParser.parse("我的工单")
    check("查看我的工单", p.intent == TicketIntent.LIST_MINE)

    p = TicketParser.parse(f"工单 {t1['ticket_id']}")
    check("查看工单详情", p.intent == TicketIntent.VIEW)

    p = TicketParser.parse("解决了")
    check("关闭意图", p.intent == TicketIntent.CLOSE)

    p = TicketParser.parse("工单统计")
    check("统计意图", p.intent == TicketIntent.STATS)

    p = TicketParser.parse("日报")
    check("日报意图", p.intent == TicketIntent.DAILY)

    p = TicketParser.parse("每日工单")
    check("每日工单意图", p.intent == TicketIntent.DAILY)

    p = TicketParser.parse("工单日报 2026-06-03")
    check("带日期日报", p.intent == TicketIntent.DAILY and p.extra == "2026-06-03")

    p = TicketParser.parse("今天下午要开会")
    check("非工单消息", p.intent == TicketIntent.NONE)

    # ── 反馈检测 ──
    intent = TicketParser.detect_feedback("是")
    check("反馈-是", intent == TicketIntent.FEEDBACK_YES)

    intent = TicketParser.detect_feedback("否")
    check("反馈-否", intent == TicketIntent.FEEDBACK_NO)

    intent = TicketParser.detect_feedback("是的")
    check("反馈-是的", intent == TicketIntent.FEEDBACK_YES)

    intent = TicketParser.detect_feedback("没有")
    check("反馈-没有", intent == TicketIntent.FEEDBACK_NO)

    intent = TicketParser.detect_feedback("yes")
    check("反馈-yes", intent == TicketIntent.FEEDBACK_YES)

    intent = TicketParser.detect_feedback("no")
    check("反馈-no", intent == TicketIntent.FEEDBACK_NO)

    # 长文本不触发反馈
    intent = TicketParser.detect_feedback("是的确实有问题需要处理一下才行")
    check("长文本非反馈", intent is None)

    # ---------------------------------------------------------------
    # 8. 反馈闭环状态流转
    # ---------------------------------------------------------------
    print("\n── 8. 反馈闭环 ──")

    # 8a. set_in_progress
    in_prog = mgr.set_in_progress(t2["ticket_id"], "user_002", "李四", "用户反馈不满意")
    check("设置处理中", in_prog is not None)
    check("状态变为 in_progress", in_prog["status"] == STATUS_IN_PROGRESS)

    # 8b. archive_as_experience（解决并由用户确认后归档为经验）
    mgr.resolve_ticket(t3["ticket_id"], "已恢复", "admin_001", "管理员")
    mgr.confirm_resolution(t3["ticket_id"], "user_003", "王五", score=5)
    archived = mgr.archive_as_experience(t3["ticket_id"], "user_003", "王五")
    check("归档工单", archived is not None)
    check("状态变为 closed", archived["status"] == STATUS_CLOSED)
    check("标记为经验", archived["is_experience"] == 1)

    # 8c. find_active_ticket_by_conv
    active = mgr.find_active_ticket_by_conv("conv_002")
    check("查找活跃工单", active is not None)
    check("找到正确工单", active["ticket_id"] == t2["ticket_id"])

    none_active = mgr.find_active_ticket_by_conv("conv_003")
    check("已归档工单不活跃", none_active is None)

    # 8d. 反馈格式化
    title, body = TicketFormatter.format_feedback_yes()
    check("反馈YES无工单", "高兴" in body or "帮到" in body)

    title, body = TicketFormatter.format_feedback_no(in_prog)
    check("反馈NO格式化", "抱歉" in body or "工单已升级" in body)
    check("反馈NO含编号", t2["ticket_id"] in body)

    title, body = TicketFormatter.format_admin_feedback_notification(
        in_prog, "李四", "ERP凭证过账报错", "AI建议检查科目余额..."
    )
    check("管理员通知格式化", "需人工处理" in body)
    check("管理员通知含用户", "李四" in body)
    check("管理员通知含工单号", t2["ticket_id"] in body)

    # ---------------------------------------------------------------
    # 9. 格式化输出
    # ---------------------------------------------------------------
    print("\n── 9. 格式化输出 ──")

    title, body = TicketFormatter.format_ticket_created(t1)
    check("创建格式化输出", "工单已创建" in body and t1["ticket_id"] in body)

    title, body = TicketFormatter.format_ticket_detail(t1, logs)
    check("详情格式化输出", t1["title"] in body)

    title, body = TicketFormatter.format_ticket_list([t1, t2])
    check("列表格式化输出", t1["ticket_id"] in body and t2["ticket_id"] in body)

    title, body = TicketFormatter.format_ticket_stats(stats)
    check("统计格式化输出", "3" in body)

    # 每日摘要格式化
    daily = mgr.get_daily_summary()
    title, body = TicketFormatter.format_daily_summary(daily)
    check("日报格式化输出", "工单日报" in body and "今日新建" in body)
    check("日报包含日期", daily["date"] in body)

    # ---------------------------------------------------------------
    # 10. 管理员
    # ---------------------------------------------------------------
    print("\n── 10. 管理员 ──")

    mgr.add_admin("admin_001", "管理员")
    check("添加管理员", mgr.is_admin("admin_001"))
    check("非管理员", not mgr.is_admin("user_001"))

    admins = mgr.list_admins()
    check("管理员列表", len(admins) == 1)

    # ---------------------------------------------------------------
    # 清理
    # ---------------------------------------------------------------
    print("\n── 清理 ──")
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
        print("  ✅ 测试数据库已删除")

    # ---------------------------------------------------------------
    # 总结
    # ---------------------------------------------------------------
    print()
    print("=" * 60)
    print(f"  📊 测试结果: {passed} 通过 / {failed} 失败")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_legacy_suite()
    sys.exit(0 if success else 1)
