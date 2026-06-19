"""
飞书 API 封装 — 纯 HTTP 调用，无状态。
所有 API 调用返回 (result, error_msg) 元组，调用方可以选择记录 error_msg。
"""
import json
import re
import time
import urllib.request
import urllib.error

BASE_URL = "https://open.feishu.cn/open-apis"

def get_tenant_token(app_id, app_secret, state=None):
    """获取 tenant_access_token。优先从 state 缓存读取，过期后重新请求。"""
    now = time.time()

    # Try cached token from state (survives across hook processes)
    if state:
        cached = state.get("feishu", {}).get("_token_cache", {})
        if cached.get("token") and now < cached.get("expires_at", 0) - 300:
            return cached["token"]

    url = f"{BASE_URL}/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            token = result["tenant_access_token"]
            expire = result.get("expire", 7200)
            # Save to state for next hook process
            if state:
                fs = state.setdefault("feishu", {})
                fs["_token_cache"] = {
                    "token": token,
                    "expires_at": now + expire,
                }
            return token
    except Exception:
        pass
    return None


def _parse_inline(text):
    """将一行文本中的 Markdown 内联标记转换为飞书 post tag 数组。
    
    支持：**bold**、*italic*、`code`、[text](url)
    """
    tags = []
    # 正则匹配：**bold** | *italic* | `code` | [text](url) | 普通文本
    pattern = re.compile(
        r'\*\*(.+?)\*\*'         # **bold**
        r'|\*(.+?)\*'            # *italic*
        r'|`(.+?)`'             # `code`
        r'|\[([^\]]+)\]\(([^)]+)\)'  # [text](url)
    )
    last = 0
    for m in pattern.finditer(text):
        # 前面的普通文本
        if m.start() > last:
            tags.append({"tag": "text", "text": text[last:m.start()]})
        if m.group(1) is not None:  # **bold**
            tags.append({"tag": "text", "text": m.group(1), "style": ["bold"]})
        elif m.group(2) is not None:  # *italic*
            tags.append({"tag": "text", "text": m.group(2), "style": ["italic"]})
        elif m.group(3) is not None:  # `code`
            tags.append({"tag": "text", "text": m.group(3), "style": ["code_inline"]})
        elif m.group(4) is not None:  # [text](url)
            href = m.group(5)
            # 飞书要求 href 是合法 URL（http/https），非法 href 会导致 230001 错误
            if href.startswith(("http://", "https://")):
                tags.append({"tag": "a", "text": m.group(4), "href": href})
            else:
                # 本地路径或非法 URL → 降级为纯文本
                tags.append({"tag": "text", "text": f"{m.group(4)}({href})"})
        last = m.end()
    # 尾部普通文本
    if last < len(text):
        tags.append({"tag": "text", "text": text[last:]})
    if not tags:
        tags.append({"tag": "text", "text": text})
    return tags


def build_post_content(text):
    """构建飞书 post 消息的 content JSON 字符串。
    
    支持特殊标记：
    - --- → hr 分割线
    - ```...``` → code_block 代码块
    - **bold** → 加粗
    - *italic* → 斜体
    - `code` → 行内代码
    - [text](url) → 链接（仅 http/https）
    """
    lines = text.split("\n")
    content_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 代码块开始标记
        if line.strip().startswith("```"):
            # 收集代码块内容
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # 跳过结束的 ```
            code_text = "\n".join(code_lines)
            content_lines.append([{"tag": "code_block", "language": "PLAINTEXT", "text": code_text}])
        elif line.strip() == "---":
            content_lines.append([{"tag": "hr"}])
            i += 1
        else:
            content_lines.append(_parse_inline(line))
            i += 1
    post = {"zh_cn": {"title": "", "content": content_lines}}
    return json.dumps(post)


def estimate_post_body_size(content_text):
    """估算 post 消息请求体大小（字节），用于 content-length 溢出预判。

    飞书 post 消息限制 30KB 请求体。此函数模拟 update_message 的请求体结构
    来估算实际大小。
    """
    content_json = build_post_content(content_text)
    body = json.dumps({"msg_type": "post", "content": content_json})
    return len(body.encode("utf-8"))


def send_message(token, chat_id, content_text):
    """POST 创建新消息，返回 (message_id, error_msg)。"""
    url = f"{BASE_URL}/im/v1/messages?receive_id_type=chat_id"
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "post",
        "content": build_post_content(content_text),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            return result["data"]["message_id"], None
        else:
            return None, f"feishu code={result.get('code')} msg={result.get('msg')} content_len={len(content_text)}"
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        return None, f"HTTP {e.code}: {body_text} content_len={len(content_text)}"
    except Exception as e:
        return None, f"Exception: {e} content_len={len(content_text)}"


def update_message(token, msg_id, content_text):
    """PUT 更新已有消息，返回 (success, error_msg)。"""
    url = f"{BASE_URL}/im/v1/messages/{msg_id}"
    body = json.dumps({
        "msg_type": "post",
        "content": build_post_content(content_text),
    }).encode()
    req = urllib.request.Request(url, data=body, method="PUT", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            return True, None
        else:
            return False, f"feishu code={result.get('code')} msg={result.get('msg')} msg_id={msg_id} content_len={len(content_text)}"
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        return False, f"HTTP {e.code}: {body_text} msg_id={msg_id} content_len={len(content_text)}"
    except Exception as e:
        return False, f"Exception: {e} msg_id={msg_id} content_len={len(content_text)}"
