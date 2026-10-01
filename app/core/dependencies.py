from fastapi import Request

from app.core.bootstrap import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
