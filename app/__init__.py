"""A-share stock query API.

勿在此包级导入 FastAPI app（会拖起 routers/agent/rag，制造假循环依赖）。
需要 ASGI 入口时用：``uvicorn app.main:app`` 或 ``from app.main import app``。
"""


from app.app import create_app

__all__ = ["create_app"]

