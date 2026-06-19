"""
飞书消息内容累积与合成。
"""

SPINNER = "\n\n⏳ 处理中..."


def compose(buffer_lines, spinner=True, footer=""):
    """将 buffer_lines 拼接成完整消息文本。

    Args:
        buffer_lines: 消息行列表 (用户 prompt、assistant 文本、工具摘要等)。
        spinner: 是否在末尾追加 ⏳。
        footer: 可选的页脚文本（如 context/cost 统计），放在 buffer 后、spinner 前。
    Returns:
        合成后的文本字符串。
    """
    text = "\n".join(buffer_lines)
    if footer:
        text += "\n" + footer
    if spinner:
        text += SPINNER
    return text
