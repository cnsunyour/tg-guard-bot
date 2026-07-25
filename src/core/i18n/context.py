"""i18n 当前异步任务上下文

ContextVar 仅作为「当前 Update 的便利默认值」来源，不是 locale 的唯一权威。
跨目的地的发送（群流程触发的私聊、定时任务、延迟任务）必须通过
``LocaleResolver`` 按目的地显式解析，不能依赖此处的上下文。
"""

from contextvars import ContextVar

current_locale: ContextVar[str | None] = ContextVar("current_locale", default=None)


def get_current_locale() -> str | None:
    """获取当前异步任务绑定的语言

    未进入 LocaleMiddleware 或显式上下文时返回 None。
    """
    return current_locale.get()
