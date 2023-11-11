#!/usr/bin/env python

import argparse
import asyncio
import asyncio.protocols
import asyncio.streams
import functools
import json
import logging.config
import os
import pathlib
import re
import signal
import subprocess
import time

import yaml

APPLICATION_NAME = "virtual-private-network-proxy"
APPLICATION_VERSION = "1.0.0"

logging.config.dictConfig(
    {
        "version": 1,
        "formatters": {"detail": {"format": "%(asctime)s %(levelname)s %(message)s"}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "detail",
            }
        },
        "root": {"level": "DEBUG", "handlers": ["console"]},
    }
)
LOGGER = logging.getLogger(APPLICATION_NAME)


class LoggingProtocol(asyncio.SubprocessProtocol):
    def pipe_data_received(self, fd, data):
        for line in data.decode("utf-8").splitlines():
            LOGGER.info(f"Process output: {line}")


class NetworkController:
    def __init__(self, loop, config):
        self.loop = loop
        self.config = config

        self.command = " ".join(
            [
                config["command"],
                *config["arguments"],
            ]
        )
        self.environment = {
            **os.environ.copy(),
            **dict([(v["name"], v["value"]) for v in config["environment"]]),
        }
        self.input = config["input"]

        self.timeout = config["timeout"]

        self.lock = asyncio.Lock()
        self.event = asyncio.Event()
        self.timestamp = time.time()

        self.transport = None

    def start(self):
        self.loop.create_task(self.__schedule_clean_up())

    def stop(self):
        self.event.set()
        self.loop.run_until_complete(self.disconnect())

    def keep_alive(self):
        self.timestamp = time.time()

    async def connect(self):
        async with self.lock:
            await self.__connect()

    async def __connect(self):
        if self.transport is None:
            LOGGER.info(f"Starting VPN connection...")
            LOGGER.info(f"Process command: {self.command}")
            self.transport, protocol = await self.loop.subprocess_shell(
                LoggingProtocol,
                self.command,
                stdin=subprocess.PIPE,
                env=self.environment,
            )
            if self.input is not None and len(self.input) > 0:
                writer = asyncio.streams.StreamWriter(
                    self.transport.get_pipe_transport(0),
                    protocol=protocol,
                    reader=None,
                    loop=self.loop,
                )
                writer.write(f"{self.input}\n".encode("utf-8"))
                writer.close()
            self.timestamp = time.time()
            await asyncio.sleep(0.5)
            LOGGER.info(f"Started VPN connection")

    async def disconnect(self):
        async with self.lock:
            await self.__disconnect()

    async def __disconnect(self):
        if self.transport is not None:
            LOGGER.info(f"Stopping VPN connection...")
            try:
                self.transport.terminate()
                # noinspection PyProtectedMember
                await asyncio.wait_for(self.transport._wait(), timeout=1)
            except ProcessLookupError:
                LOGGER.info("No such process")
            except asyncio.TimeoutError:
                LOGGER.warning(f"Failed to terminate transport: Killing transport...")
                self.transport.kill()
                # noinspection PyProtectedMember
                await self.transport._wait()
            finally:
                self.transport = None
            LOGGER.info(f"Stopped VPN connection")

    async def __schedule_clean_up(self):
        LOGGER.info("Starting clean up scheduler...")
        while not self.event.is_set():
            try:
                await asyncio.wait_for(self.event.wait(), 1)
            except asyncio.TimeoutError:
                pass
            async with self.lock:
                await self.__clean_up()
        LOGGER.info("Stopped clean up scheduler")

    async def __clean_up(self):
        try:
            t = int(time.time() - self.timestamp)
            if self.transport is not None:
                if t > 0 and t % 10 == 0:
                    LOGGER.info(f"VPN connection idle: {t}s")
                if t > self.timeout:
                    await self.__disconnect()
        except Exception as exception:
            LOGGER.info(f"VPN connection clean up task failed", exc_info=True)
            raise exception


class ProxyServer:
    def __init__(self, name, server, port, controller):
        self.name = name
        self.server = server
        self.port = port

        self.controller = controller

    async def handle_connection(self, server_reader, server_writer):
        server_client_ip, server_client_port = server_writer.transport.get_extra_info(
            "socket"
        ).getpeername()
        server_local_ip, server_local_port = server_writer.transport.get_extra_info(
            "socket"
        ).getsockname()
        LOGGER.debug(
            f"[{self.name}] New server connection: {server_client_ip}:{server_client_port} → {server_local_ip}:{server_local_port}"
        )
        try:
            await self.controller.connect()
            remote_reader, remote_writer = None, None
            for i in range(10, 0, -1):
                try:
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(self.server, self.port), 0.5
                    )
                    break
                except (asyncio.TimeoutError, OSError):
                    LOGGER.debug(f"[{self.name}] Connection failed: Sleeping 500ms...")
                    await asyncio.sleep(0.5)
            if remote_reader is None or remote_writer is None:
                raise Exception("Retries exceeded")
            remote_local_ip, remote_local_port = remote_writer.transport.get_extra_info(
                "socket"
            ).getsockname()
            (
                remote_server_ip,
                remote_server_port,
            ) = remote_writer.transport.get_extra_info("socket").getpeername()
            LOGGER.debug(
                f"[{self.name}] New remote connection: {remote_local_ip}:{remote_local_port} → {remote_server_ip}:{remote_server_port}"
            )
            pipes = [
                self.__pipe_data(server_reader, remote_writer, False),
                self.__pipe_data(remote_reader, server_writer, True),
            ]
            await asyncio.gather(*pipes)
        except OSError as error:
            LOGGER.info(f"Connection error", exc_info=True)
            await self.controller.disconnect()
        except Exception as ex:
            LOGGER.warning(f"Unknown exception", exc_info=True)
            await self.controller.disconnect()
            raise ex
        finally:
            server_writer.close()

    async def __pipe_data(self, reader, writer, server=False):
        try:
            while not reader.at_eof():
                writer.write(await reader.read(4096))
                self.controller.keep_alive()
        finally:
            reader_ip, reader_port = writer.transport.get_extra_info(
                "socket"
            ).getsockname()
            writer_ip, writer_port = writer.transport.get_extra_info(
                "socket"
            ).getpeername()
            writer.close()
            if server:
                LOGGER.debug(
                    f"[{self.name}] Closed server connection: {writer_ip}:{writer_port} → {reader_ip}:{reader_port}"
                )
            else:
                LOGGER.debug(
                    f"[{self.name}] Closed remote connection: {reader_ip}:{reader_port} → {writer_ip}:{writer_port}"
                )


def start():
    LOGGER.info(f"Starting {APPLICATION_NAME}...")
    LOGGER.info(f"Version: {APPLICATION_VERSION}")

    parser = argparse.ArgumentParser(description=APPLICATION_NAME)
    parser.add_argument(
        "-v", "--version", action="version", version=APPLICATION_VERSION
    )
    parser.add_argument("-c", "--config", type=str, required=True)
    args = parser.parse_args()

    with pathlib.Path(args.config).open(encoding="utf-8") as file:
        text = file.read()
    for match in re.findall(r"\${[A-Z_]+}", text):
        if value := os.environ.get(name := match[2:-1]):
            text = text.replace(match, value)
        else:
            raise Exception(f"Environment variable not set: {name}")
    config = yaml.load(text, yaml.FullLoader)
    LOGGER.info(f"Proxy config: {json.dumps(config['proxy'])}")
    LOGGER.info(
        f"VPN config: {json.dumps({k: config['vpn'][k] for k in config['vpn'] if k not in ['input', 'files'] })}"
    )

    paths = []
    if "files" in config["vpn"]:
        for file in config["vpn"]["files"]:
            path = pathlib.Path(file["path"])
            with path.open("w") as fd:
                fd.write(file["content"])
            if file.get("executable", False):
                path.chmod(0o500)
            else:
                path.chmod(0o400)
            paths.append(path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    controller = NetworkController(loop, config["vpn"])
    controller.start()

    servers = []
    for listener in config["proxy"]["listeners"]:
        name = listener["name"]
        interface = "0.0.0.0"
        port = listener["ports"]["local"]
        proxy = ProxyServer(
            name, config["proxy"]["server"], listener["ports"]["remote"], controller
        )
        # noinspection PyTypeChecker
        servers.append(
            loop.run_until_complete(
                asyncio.start_server(
                    functools.partial(proxy.handle_connection),
                    interface,
                    port,
                )
            )
        )
        LOGGER.info(f"[{name}] Serving: {interface}:{port}")

    # noinspection PyUnusedLocal
    def signal_handler(sig, action):
        LOGGER.info(f"Received signal: {signal.Signals(sig).name}")
        loop.call_soon_threadsafe(loop.stop)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    loop.run_forever()

    for server in servers:
        server.close()
        loop.run_until_complete(server.wait_closed())
    controller.stop()
    loop.close()

    for path in paths:
        path.unlink()

    LOGGER.info(f"Stopped {APPLICATION_NAME}")


if __name__ == "__main__":
    start()
