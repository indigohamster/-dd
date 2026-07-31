# ============================================================
# 钉钉 OpenAPI 客户端 - 文件/图片/富文本发送
# ============================================================
#
# 基于 DingTalk OpenAPI 2.0，复用应用凭证获取 access_token，
# 支持上传文件/图片、发送富文本/文件消息到群聊或单聊。
#
# API 文档:
#   - 获取 access_token: GET  https://oapi.dingtalk.com/gettoken
#   - 上传媒体文件:     POST https://oapi.dingtalk.com/media/upload
#   - 发送群聊消息:     POST https://api.dingtalk.com/v1.0/robot/groupMessages/send
#   - 发送单聊消息:     POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend
# ============================================================

import os
import json
import time
import logging
import threading
import requests


class DingTalkAPI:
    """
    钉钉 OpenAPI 客户端（新版 v1.0）。
    用于发送文件、图片、富文本等非纯文本消息。
    """

    # 旧版域名（仅用于获取 access_token）
    OAPI_URL = "https://oapi.dingtalk.com"
    # 新版域名（用于 v1.0 API）
    API_URL = "https://api.dingtalk.com"

    def __init__(self, client_id: str, client_secret: str, agent_id: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.agent_id = agent_id
        self.logger = logging.getLogger("DingTalkAPI")
        self._token = ""
        self._token_expires = 0
        self.__token_lock__ = threading.Lock()
        self._MAX_TOKEN_RETRY = 2

    # ------------------------------------------------------------------
    # Token 管理
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        """
        获取或刷新 access_token（线程安全 + 2次重试）。
        Token 有效期 7200 秒，提前 5 分钟刷新。
        """
        # 快速路径：已有有效 token，无需加锁
        if self._token and time.time() < self._token_expires - 300:
            return self._token

        # 慢速路径：双重检查锁
        with self.__token_lock__:
            # 再次检查（其他线程可能已刷新）
            if self._token and time.time() < self._token_expires - 300:
                return self._token

            url = f"{self.OAPI_URL}/gettoken"
            params = {
                "appkey": self.client_id,
                "appsecret": self.client_secret,
            }

            last_err = None
            for attempt in range(self._MAX_TOKEN_RETRY + 1):
                try:
                    resp = requests.get(url, params=params, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()

                    try:
                        from utils.api_tracker import api_tracker
                        api_tracker.record("钉钉 OpenAPI", "/gettoken", True, 0)
                    except Exception:
                        pass

                    if data.get("errcode") != 0:
                        raise RuntimeError(f"获取 access_token 失败: {data}")

                    self._token = data["access_token"]
                    self._token_expires = time.time() + data.get("expires_in", 7200)
                    self.logger.info("access_token 刷新成功")
                    return self._token

                except Exception as e:
                    last_err = e
                    self.logger.warning(f"获取 access_token 第 {attempt + 1} 次失败: {e}")
                    if attempt < self._MAX_TOKEN_RETRY:
                        time.sleep(1 * (attempt + 1))  # 指数退避

            raise RuntimeError(f"获取 access_token 失败（已重试 {self._MAX_TOKEN_RETRY} 次）: {last_err}")

    # ------------------------------------------------------------------
    # 文件上传
    # ------------------------------------------------------------------

    def upload_file(self, file_path: str, file_type: str = "file") -> str:
        """
        上传文件到钉钉服务器，返回 downloadCode。
        file_type: "file"（普通文件）, "image"（图片）
        """
        token = self.get_access_token()
        url = f"{self.OAPI_URL}/media/upload"
        params = {
            "access_token": token,
            "type": file_type,
        }

        file_name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                params=params,
                files={"media": (file_name, f)},
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode") != 0:
            raise RuntimeError(f"文件上传失败: {data}")

        media_id = data.get("media_id", "")
        self.logger.info(f"文件上传成功: {file_name} -> media_id={media_id}")
        try:
            from utils.api_tracker import api_tracker
            api_tracker.record("钉钉 OpenAPI", "/media/upload", True, 0)
        except Exception:
            pass
        return media_id

    # ------------------------------------------------------------------
    # 发送文件消息到群聊（新版 v1.0 机器人接口）
    # ------------------------------------------------------------------

    def send_file_to_chat(self, open_conversation_id: str, media_id: str, file_name: str = "") -> dict:
        """
        通过机器人发送文件消息到群聊。
        新版 API: POST /v1.0/robot/groupMessages/send
        群聊参数为 openConversationId（非 conversationId）
        """
        token = self.get_access_token()
        url = f"{self.API_URL}/v1.0/robot/groupMessages/send"
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }

        ext = os.path.splitext(file_name)[1].lstrip(".").lower() if file_name else ""
        payload = {
            "robotCode": self.client_id,
            "openConversationId": open_conversation_id,
            "msgKey": "sampleFile",
            "msgParam": json.dumps({
                "mediaId": media_id,
                "fileName": file_name or "file",
                "fileType": ext or "file",
            }),
        }

        self.logger.info(f"[send_file_to_chat] robotCode={self.client_id}, openConversationId={open_conversation_id[:20] if open_conversation_id else 'N/A'}...")
        self.logger.info(f"[send_file_to_chat] payload={json.dumps(payload, ensure_ascii=False)[:300]}")

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            self.logger.error(f"[send_file_to_chat] HTTP {resp.status_code}: {resp.text}")
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("钉钉 OpenAPI", "/robot/groupMessages/send", False, 0)
            except Exception:
                pass
        resp.raise_for_status()
        data = resp.json()
        self.logger.info(f"群聊文件消息发送结果: {data}")
        try:
            from utils.api_tracker import api_tracker
            api_tracker.record("钉钉 OpenAPI", "/robot/groupMessages/send", True, 0)
        except Exception:
            pass
        return data

    # ------------------------------------------------------------------
    # 发送文件消息到单聊（新版 v1.0 机器人接口）
    # ------------------------------------------------------------------

    def send_file_to_user(self, user_id: str, media_id: str, file_name: str = "") -> dict:
        """
        通过机器人发送文件消息到单聊。
        新版 API: POST /v1.0/robot/oToMessages/batchSend
        官方 SDK 参数名为 userIds（非 receiverUserIds）
        """
        token = self.get_access_token()
        url = f"{self.API_URL}/v1.0/robot/oToMessages/batchSend"
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }

        ext = os.path.splitext(file_name)[1].lstrip(".").lower() if file_name else ""
        payload = {
            "robotCode": self.client_id,
            "conversationType": 1,
            "userIds": [user_id],
            "msgKey": "sampleFile",
            "msgParam": json.dumps({
                "mediaId": media_id,
                "fileName": file_name or "file",
                "fileType": ext or "file",
            }),
        }

        self.logger.info(f"[send_file_to_user] robotCode={self.client_id}, userIds=[{user_id[:10]}...], msgKey=sampleFile")
        self.logger.info(f"[send_file_to_user] payload={json.dumps(payload, ensure_ascii=False)[:300]}")

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            self.logger.error(f"[send_file_to_user] HTTP {resp.status_code}: {resp.text}")
            try:
                from utils.api_tracker import api_tracker
                api_tracker.record("钉钉 OpenAPI", "/robot/oToMessages/batchSend", False, 0)
            except Exception:
                pass
        resp.raise_for_status()
        data = resp.json()
        self.logger.info(f"单聊文件消息发送结果: {data}")
        try:
            from utils.api_tracker import api_tracker
            api_tracker.record("钉钉 OpenAPI", "/robot/oToMessages/batchSend", True, 0)
        except Exception:
            pass
        return data

    # ------------------------------------------------------------------
    # 发送 Markdown 消息（群聊/单聊）
    # ------------------------------------------------------------------

    def send_markdown_to_chat(self, open_conversation_id: str, title: str, text: str) -> dict:
        """
        通过机器人发送 Markdown 消息到群聊。
        新版 API: POST /v1.0/robot/groupMessages/send
        群聊参数为 openConversationId（非 conversationId）

        注意：仅适用于群聊（conversationType=2），单聊请用 send_markdown_to_user。
        """
        token = self.get_access_token()
        url = f"{self.API_URL}/v1.0/robot/groupMessages/send"
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }

        # 截断过长的 Markdown 内容（钉钉限制约 20000 字符）
        MAX_TEXT_LEN = 16000
        MAX_TITLE_LEN = 50
        if len(title) > MAX_TITLE_LEN:
            title = title[:MAX_TITLE_LEN - 3] + "..."
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN - 20] + "\n\n> ...（内容过长已截断）"

        payload = {
            "robotCode": self.client_id,
            "openConversationId": open_conversation_id,
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": text}),
        }

        self.logger.info(
            f"[send_markdown_to_chat] robotCode={self.client_id}, "
            f"convId={open_conversation_id[:20]}..., "
            f"title={title[:30]}, text_len={len(text)}"
        )

        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            self.logger.error(
                f"[send_markdown_to_chat] HTTP {resp.status_code}: {resp.text[:500]}"
            )
        resp.raise_for_status()
        data = resp.json()
        self.logger.info(f"群聊 Markdown 消息发送结果: {data}")
        return data

    def send_markdown_to_user(self, user_id: str, title: str, text: str) -> dict:
        """
        通过机器人发送 Markdown 消息到单聊（私聊）。
        新版 API: POST /v1.0/robot/oToMessages/batchSend
        """
        token = self.get_access_token()
        url = f"{self.API_URL}/v1.0/robot/oToMessages/batchSend"
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }

        # 截断过长的 Markdown 内容
        MAX_TEXT_LEN = 16000
        MAX_TITLE_LEN = 50
        if len(title) > MAX_TITLE_LEN:
            title = title[:MAX_TITLE_LEN - 3] + "..."
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN - 20] + "\n\n> ...（内容过长已截断）"

        payload = {
            "robotCode": self.client_id,
            "conversationType": 1,
            "userIds": [user_id],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": text}),
        }

        self.logger.info(
            f"[send_markdown_to_user] userIds=[{user_id[:10]}...], title={title[:30]}, text_len={len(text)}"
        )
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code != 200:
            self.logger.error(
                f"[send_markdown_to_user] HTTP {resp.status_code}: {resp.text[:500]}"
            )
        resp.raise_for_status()
        data = resp.json()
        self.logger.info(f"单聊 Markdown 消息发送结果: {data}")
        return data

    # ------------------------------------------------------------------
    # 便捷方法：上传并发送文件
    # ------------------------------------------------------------------

    def upload_and_send_file(
        self,
        file_path: str,
        conversation_id: str,
        conv_type: str = "2",
        user_id: str = "",
        file_type: str = "file",
        file_name: str = "",
    ) -> dict:
        """
        一站式：上传文件 + 发送到会话。
        conv_type: "1"=单聊, "2"=群聊
        """
        media_id = self.upload_file(file_path, file_type)

        if conv_type == "2":
            return self.send_file_to_chat(conversation_id, media_id, file_name)
        elif conv_type == "1" and user_id:
            return self.send_file_to_user(user_id, media_id, file_name)
        else:
            raise ValueError("单聊需要提供 user_id")

    def test_connection(self) -> bool:
        """测试 API 连接是否正常"""
        try:
            token = self.get_access_token()
            self.logger.info(f"API 连接测试成功, token 前缀: {token[:10]}...")
            return True
        except Exception as e:
            self.logger.error(f"API 连接测试失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 下载机器人消息中的文件/图片/语音
    # ------------------------------------------------------------------

    def download_message_file(self, download_code: str, save_path: str) -> str:
        """
        下载机器人消息中的文件/图片/语音。

        API: POST /v1.0/robot/messageFiles/download
        返回临时下载 URL，然后下载文件保存到本地。

        :param download_code: 消息中的 downloadCode
        :param save_path: 本地保存路径
        :return: 保存路径
        """
        token = self.get_access_token()

        # Step 1: 获取临时下载 URL
        url = f"{self.API_URL}/v1.0/robot/messageFiles/download"
        headers = {
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        }
        payload = {
            "downloadCode": download_code,
            "robotCode": self.client_id,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        download_url = data.get("downloadUrl", "")
        if not download_url:
            raise RuntimeError(f"获取下载链接失败: {data}")

        # Step 2: 下载文件
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        resp = requests.get(download_url, timeout=60)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(resp.content)

        file_size = len(resp.content)
        self.logger.info(f"文件下载成功: {save_path} ({file_size} bytes)")
        try:
            from utils.api_tracker import api_tracker
            api_tracker.record("钉钉 OpenAPI", "/robot/messageFiles/download", True, 0)
        except Exception:
            pass
        return save_path

    # ------------------------------------------------------------------
    # 通讯录查询：根据昵称查找企业 userId（staffId）
    # ------------------------------------------------------------------

    def _resolve_staff_id_by_nick_old(self, nick: str, dept_id: int = 1) -> str:
        """
        通过用户昵称在通讯录中查找企业 userId（staffId）。

        调用 /topapi/v2/user/list 按部门遍历，匹配 name 字段。
        dept_id=1 表示根部门。

        需要应用具备「通讯录部门用户读权限」。
        """
        token = self.get_access_token()
        url = f"{self.OAPI_URL}/topapi/v2/user/list"
        params = {"access_token": token}

        cursor = 0
        size = 100
        while True:
            payload = {
                "dept_id": dept_id,
                "cursor": cursor,
                "size": size,
            }
            resp = requests.post(url, params=params, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("errcode") != 0:
                self.logger.warning(
                    f"通讯录查询失败: errcode={data.get('errcode')}, "
                    f"errmsg={data.get('errmsg', '')}"
                )
                return ""

            for user in data.get("result", {}).get("list", []):
                # 匹配昵称（精确匹配）
                if user.get("name") == nick:
                    staff_id = user.get("userid", "")
                    self.logger.info(
                        f"通讯录匹配成功: name={nick}, userid={staff_id}"
                    )
                    return staff_id

            # 是否还有下一页
            has_more = data.get("result", {}).get("has_more", False)
            if not has_more:
                break
            cursor = data.get("result", {}).get("next_cursor", 0)

        self.logger.warning(f"通讯录中未找到昵称为 '{nick}' 的用户")
        return ""

    def resolve_staff_id_by_open_id(self, open_id: str) -> str:
        """
        通过用户的 openId（LWCP 格式）查询企业 userId（staffId）。

        尝试调用 /topapi/user/getbyunionid 接口。
        注意：Stream SDK 的 sender_id 可能是 unionId 而非 openId，
        如果失败则回退到昵称查询。

        需要应用具备「通讯录个人信息读权限」。
        """
        token = self.get_access_token()

        # 尝试1: 用 sender_id 作为 unionId 查询
        url = f"{self.OAPI_URL}/topapi/user/getbyunionid"
        params = {"access_token": token}
        payload = {"unionid": open_id}
        try:
            resp = requests.post(url, params=params, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") == 0:
                result = data.get("result", {})
                staff_id = result.get("userid", "")
                if staff_id:
                    self.logger.info(
                        f"通过 unionId 查询成功: openId={open_id[:15]}..., "
                        f"userid={staff_id}"
                    )
                    return staff_id
            self.logger.info(
                f"unionId 查询未命中: errcode={data.get('errcode')}, "
                f"errmsg={data.get('errmsg', '')[:100]}"
            )
        except Exception as e:
            self.logger.warning(f"unionId 查询异常: {e}")

        return ""
