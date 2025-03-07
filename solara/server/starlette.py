import logging
import os
import pathlib
import sys
import typing
from typing import List, Union, cast
from uuid import uuid4

import anyio
import starlette.websockets
import uvicorn.server
import websockets.legacy.http
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

import solara
from solara.server.threaded import ServerBase

from . import app as appmod
from . import server, settings, telemetry, websocket
from .cdn_helper import cdn_url_path, default_cache_dir, get_path

os.environ["SERVER_SOFTWARE"] = "solara/" + str(solara.__version__)

logger = logging.getLogger("solara.server.fastapi")
# if we add these to the router, the server_test does not run (404's)
prefix = ""

# The limit for starlette's http traffic should come from h11's DEFAULT_MAX_INCOMPLETE_EVENT_SIZE=16kb
# In practice, testing with 132kb cookies (server_test.py:test_large_cookie) seems to work fine.
# For the websocket, the limit is set to 4kb till 10.4, see
#  * https://github.com/aaugustin/websockets/blob/10.4/src/websockets/legacy/http.py#L14
# Later releases should set this to 8kb. See
#  * https://github.com/aaugustin/websockets/commit/8ce4739b7efed3ac78b287da7fb5e537f78e72aa
#  * https://github.com/aaugustin/websockets/issues/743
# Since starlette seems to accept really large values for http, lets do the same for websockets
# An arbitrarily large value we settled on for now is 32kb
# If we don't do this, users with many cookies will fail to get a websocket connection.
websockets.legacy.http.MAX_LINE = 1024 * 32


class WebsocketWrapper(websocket.WebsocketWrapper):
    ws: starlette.websockets.WebSocket

    def __init__(self, ws: starlette.websockets.WebSocket, portal: anyio.from_thread.BlockingPortal) -> None:
        self.ws = ws
        self.portal = portal

    def close(self):
        self.portal.call(self.close)

    def send_text(self, data: str) -> None:
        self.portal.call(self.ws.send_text, data)

    def send_bytes(self, data: bytes) -> None:
        self.portal.call(self.ws.send_bytes, data)

    def receive(self):
        message = self.portal.call(self.ws.receive)
        if "text" in message:
            return message["text"]
        elif "bytes" in message:
            return message["bytes"]
        elif message.get("type") == "websocket.disconnect":
            raise websocket.WebSocketDisconnect()
        else:
            raise RuntimeError(f"Unknown message type {message}")


class ServerStarlette(ServerBase):
    server: uvicorn.server.Server

    def has_started(self):
        return self.server.started

    def signal_stop(self):
        self.server.should_exit = True
        # this cause uvicorn to not wait for background tasks, e.g.:
        # <Task pending name='Task-55'
        #  coro=<WebSocketProtocol.run_asgi() running at
        #  /.../uvicorn/protocols/websockets/websockets_impl.py:184>
        # wait_for=<Future pending cb=[<TaskWakeupMethWrapper object at 0x16896aa00>()]>
        # cb=[WebSocketProtocol.on_task_complete()]>
        self.server.force_exit = True
        self.server.lifespan.should_exit = True

    def serve(self):

        from uvicorn.config import Config
        from uvicorn.server import Server

        if sys.version_info[:2] < (3, 7):
            # make python 3.6 work
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # uvloop will trigger a: RuntimeError: There is no current event loop in thread 'fastapi-thread'
        config = Config(app, host=self.host, port=self.port, **self.kwargs, loop="asyncio")
        self.server = Server(config=config)
        self.started.set()
        self.server.run()


async def kernels(id):
    return JSONResponse({"name": "lala", "id": "dsa"})


async def kernel_connection(ws: starlette.websockets.WebSocket):
    session_id = ws.cookies.get(server.COOKIE_KEY_SESSION_ID)
    if not session_id:
        logger.error("no session cookie")
        await ws.close()
        return
    connection_id = ws.query_params["session_id"]
    if not connection_id:
        logger.error("no session_id/connection_id")
        await ws.close()
        return
    logger.info("Solara kernel requested for session_id=%s connection_id=%s", session_id, connection_id)
    await ws.accept()

    def websocket_thread_runner(ws: starlette.websockets.WebSocket, portal: anyio.from_thread.BlockingPortal):
        ws_wrapper = WebsocketWrapper(ws, portal)

        async def run():
            try:
                assert session_id is not None
                assert connection_id is not None
                await server.app_loop(ws_wrapper, session_id, connection_id)
            except:  # noqa
                await portal.stop(cancel_remaining=True)
                raise

        # sometimes throws: RuntimeError: Already running asyncio in this thread
        anyio.run(run)

    # this portal allows us to sync call the websocket calls from this current event loop we are in
    # each websocket however, is handled from a separate thread
    try:
        async with anyio.from_thread.BlockingPortal() as portal:
            thread_return = anyio.to_thread.run_sync(websocket_thread_runner, ws, portal)
            await thread_return
    finally:
        try:
            await ws.close()
        except:  # noqa
            pass


async def close(request: Request):
    connection_id = request.path_params["connection_id"]
    if connection_id in appmod.contexts:
        context = appmod.contexts[connection_id]
        context.close()
    response = HTMLResponse(content="", status_code=200)
    return response


async def root(request: Request, fullpath: str = ""):
    root_path = settings.main.root_path or ""
    # if not explicltly set,
    if settings.main.root_path is None:
        # use the default root path from the app, which seems to also include the path
        # if we are mounted under a path
        root_path = request.scope.get("root_path", "")
        logger.debug("root_path: %s", root_path)
        # or use the script-name header, for instance when running under a reverse proxy
        script_name = request.headers.get("script-name")
        if script_name:
            logger.debug("override root_path using script-name header from %s to %s", root_path, script_name)
            root_path = script_name
        script_name = request.headers.get("x-script-name")
        if script_name:
            logger.debug("override root_path using x-script-name header from %s to %s", root_path, script_name)
            root_path = script_name
        settings.main.root_path = root_path

    request_path = request.url.path
    if request_path.startswith(root_path):
        request_path = request_path[len(root_path) :]
    content = server.read_root(request_path, root_path)
    if content is None:
        return HTMLResponse(content="Page not found by Solara router", status_code=404)
    response = HTMLResponse(content=content)
    session_id = request.cookies.get(server.COOKIE_KEY_SESSION_ID) or str(uuid4())
    samesite = "lax"
    secure = False
    # we want samesite, so we can set a cookie when embedded in an iframe, such as on huggingface
    # however, samesite=none requires Secure https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie/SameSite
    if request.headers.get("x-forwarded-proto", "http") == "https":
        samesite = "none"
        secure = True
    response.set_cookie(
        server.COOKIE_KEY_SESSION_ID, value=session_id, expires="Fri, 01 Jan 2038 00:00:00 GMT", samesite=samesite, secure=secure  # type: ignore
    )  # type: ignore
    return response


class StaticNbFiles(StaticFiles):
    def get_directories(
        self,
        directory: Union[str, "os.PathLike[str]", None] = None,
        packages=None,  # type: ignore
    ) -> List[Union[str, "os.PathLike[str]"]]:
        return cast(List[Union[str, "os.PathLike[str]"]], server.nbextensions_directories)

    # follow symlinks
    # from https://github.com/encode/starlette/pull/1377/files
    def lookup_path(self, path: str) -> typing.Tuple[str, typing.Optional[os.stat_result]]:
        for directory in self.all_directories:
            original_path = os.path.join(directory, path)
            full_path = os.path.realpath(original_path)
            directory = os.path.realpath(directory)
            try:
                return full_path, os.stat(full_path)
            except (FileNotFoundError, NotADirectoryError):
                continue
        return "", None


class StaticPublic(StaticFiles):
    def lookup_path(self, *args, **kwargs):
        self.all_directories = self.get_directories(None, None)
        return super().lookup_path(*args, **kwargs)

    def get_directories(
        self,
        directory: Union[str, "os.PathLike[str]", None] = None,
        packages=None,  # type: ignore
    ) -> List[Union[str, "os.PathLike[str]"]]:
        # we only know the .directory at runtime (after startup)
        # which means we cannot pass the directory to the StaticFiles constructor
        return cast(List[Union[str, "os.PathLike[str]"]], [app.directory.parent / "public" for app in appmod.apps.values()])


class StaticAssets(StaticFiles):
    def lookup_path(self, *args, **kwargs):
        self.all_directories = self.get_directories(None, None)
        return super().lookup_path(*args, **kwargs)

    def get_directories(
        self,
        directory: Union[str, "os.PathLike[str]", None] = None,
        packages=None,  # type: ignore
    ) -> List[Union[str, "os.PathLike[str]"]]:
        # we only know the .directory at runtime (after startup)
        # which means we cannot pass the directory to the StaticFiles constructor
        overrides = [app.directory.parent / "assets" for app in appmod.apps.values()]
        default = server.solara_static.parent / "assets"
        return cast(List[Union[str, "os.PathLike[str]"]], [*overrides, default])


class StaticCdn(StaticFiles):
    def lookup_path(self, path: str) -> typing.Tuple[str, typing.Optional[os.stat_result]]:
        full_path = get_path(pathlib.Path(default_cache_dir), path)
        return full_path, os.stat(full_path)


def on_startup():
    # TODO: configure and set max number of threads
    # see https://github.com/encode/starlette/issues/1724
    telemetry.server_start()


def on_shutdown():
    telemetry.server_stop()


def readyz(request: Request):
    json, status = server.readyz()
    return JSONResponse(json, status_code=status)


middleware = [Middleware(GZipMiddleware, minimum_size=1000)]

routes = [
    Route("/readyz", endpoint=readyz),
    Route("/jupyter/api/kernels/{id}", endpoint=kernels),
    WebSocketRoute("/jupyter/api/kernels/{id}/{name}", endpoint=kernel_connection),
    Route("/", endpoint=root),
    Route("/{fullpath}", endpoint=root),
    Route("/_solara/api/close/{connection_id}", endpoint=close, methods=["POST"]),
    Mount(f"/{cdn_url_path}", app=StaticCdn(directory=default_cache_dir)),
    Mount(f"{prefix}/static/public", app=StaticPublic()),
    Mount(f"{prefix}/static/assets", app=StaticAssets()),
    Mount(f"{prefix}/static/nbextensions", app=StaticNbFiles()),
    Mount(f"{prefix}/static/nbconvert", app=StaticFiles(directory=server.nbconvert_static)),
    Mount(f"{prefix}/static", app=StaticFiles(directory=server.solara_static)),
    Route("/{fullpath:path}", endpoint=root),
]

app = Starlette(routes=routes, on_startup=[on_startup], on_shutdown=[on_shutdown], middleware=middleware)
# Uncomment the lines below to test solara mouted under a subpath
# def myroot(request: Request):
#     return JSONResponse({"framework": "solara"})

# routes_test_sub = [Route("/", endpoint=myroot), Mount("/foo/", routes=routes)]
# app = Starlette(routes=routes_test_sub, on_startup=[on_startup], on_shutdown=[on_shutdown], middleware=middleware)
