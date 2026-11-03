import asyncio
import base64
import json
import threading
import uuid
from datetime import datetime, timezone, timedelta

import requests
import websockets
from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.properties import StringProperty, ListProperty
from kivy.uix.screenmanager import Screen, ScreenManager

REGISTRY_FILE = "servers/registry.json"
PUBLISH_SECONDS = 300
DISCOVERY_SECONDS = 300
REGISTRY_TTL_MINUTES = 15
GITHUB_API = "https://api.github.com"

KV = r"""
<MainScreen>:
    BoxLayout:
        orientation: "vertical"
        padding: "12dp"
        spacing: "7dp"
        canvas.before:
            Color:
                rgba: (.025, .04, .075, 1)
            Rectangle:
                pos: self.pos
                size: self.size
        BoxLayout:
            size_hint_y: None
            height: "44dp"
            Label:
                text: "[b]NEXUS[/b] [color=55B7FF]NETWORK[/color]"
                markup: True
                font_size: "21sp"
                halign: "left"
            Label:
                text: root.status
                color: root.status_color
                font_size: "12sp"
        Spinner:
            id: role
            text: "Клиент"
            values: ["Клиент", "Сервер"]
            size_hint_y: None
            height: "42dp"
        GridLayout:
            cols: 2
            size_hint_y: None
            height: "90dp"
            spacing: "5dp"
            TextInput:
                id: owner
                hint_text: "GitHub owner"
                multiline: False
            TextInput:
                id: repo
                hint_text: "Private registry repo"
                text: "NEXUS-Registry"
                multiline: False
            TextInput:
                id: gh_token
                hint_text: "GitHub token (сервер RW / клиент Read)"
                password: True
                multiline: False
            TextInput:
                id: session_key
                hint_text: "Общий ключ NEXUS"
                password: True
                multiline: False
        BoxLayout:
            orientation: "vertical"
            size_hint_y: None
            height: "132dp" if role.text == "Сервер" else "0dp"
            opacity: 1 if role.text == "Сервер" else 0
            spacing: "4dp"
            TextInput:
                id: server_id
                hint_text: "Уникальный Server ID"
                text: "nexus-" + app.install_id
                multiline: False
                size_hint_y: None
                height: "40dp"
            TextInput:
                id: server_name
                hint_text: "Название сервера"
                text: "NEXUS Server"
                multiline: False
                size_hint_y: None
                height: "40dp"
            TextInput:
                id: public_host
                hint_text: "Публичный IP / DNS / VPN-адрес"
                multiline: False
                size_hint_y: None
                height: "40dp"
            BoxLayout:
                size_hint_y: None
                height: "40dp"
                TextInput:
                    id: listen_host
                    text: "0.0.0.0"
                    hint_text: "Listen host"
                    multiline: False
                TextInput:
                    id: port
                    text: "8765"
                    hint_text: "Port"
                    input_filter: "int"
                    multiline: False
        BoxLayout:
            size_hint_y: None
            height: "42dp" if role.text == "Клиент" else "0dp"
            opacity: 1 if role.text == "Клиент" else 0
            TextInput:
                id: preferred_id
                hint_text: "Server ID (пусто = первый доступный)"
                multiline: False
        Button:
            text: "ЗАПУСТИТЬ СЕРВЕР" if role.text == "Сервер" else "НАЙТИ СЕРВЕР"
            size_hint_y: None
            height: "46dp"
            background_color: (.08, .40, .76, 1)
            on_release: app.start_action(role.text)
        Button:
            text: "ОСТАНОВИТЬ / ОТКЛЮЧИТЬ"
            size_hint_y: None
            height: "42dp"
            background_color: (.55, .16, .20, 1)
            on_release: app.stop_action()
        Label:
            text: "СЕТЕВОЙ ЖУРНАЛ"
            size_hint_y: None
            height: "20dp"
            color: (.40, .70, .98, 1)
            halign: "left"
        ScrollView:
            do_scroll_x: False
            Label:
                text: app.log_text
                size_hint_y: None
                height: self.texture_size[1] + "16dp"
                text_size: self.width, None
                valign: "top"
                halign: "left"
        BoxLayout:
            size_hint_y: None
            height: "44dp"
            spacing: "5dp"
            TextInput:
                id: message
                hint_text: "Сообщение..."
                multiline: False
            Button:
                text: "SEND"
                size_hint_x: .25
                on_release: app.send_message(message.text); message.text = ""
"""

class MainScreen(Screen):
    status = StringProperty("OFFLINE")
    status_color = ListProperty([1, .65, .25, 1])

class NexusApp(App):
    log_text = StringProperty("NEXUS Network 1.0\n")
    install_id = str(uuid.uuid4())[:8]

    def build(self):
        Builder.load_string(KV)
        self.screen = MainScreen(name="main")
        manager = ScreenManager()
        manager.add_widget(self.screen)
        self.loop = None
        self.loop_thread = None
        self.mode = None
        self.active = False
        self.server = None
        self.client_ws = None
        self.peers = {}
        self.config = {}
        self.stop_requested = False
        return manager

    def log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        def add(_):
            self.log_text += f"[{stamp}] {message}\n"
            if len(self.log_text) > 12000:
                self.log_text = self.log_text[-8500:]
        Clock.schedule_once(add)

    def set_status(self, value, online=False):
        def apply(_):
            self.screen.status = value
            self.screen.status_color = [.25, .9, .55, 1] if online else [1, .65, .25, 1]
        Clock.schedule_once(apply)

    def ensure_loop(self):
        if self.loop and self.loop.is_running():
            return
        self.loop = asyncio.new_event_loop()
        def runner():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        self.loop_thread = threading.Thread(target=runner, daemon=True)
        self.loop_thread.start()

    def start_action(self, role):
        if self.active:
            self.log("Сначала остановите текущий режим.")
            return
        ids = self.screen.ids
        owner = ids.owner.text.strip()
        repo = ids.repo.text.strip()
        token = ids.gh_token.text.strip()
        session = ids.session_key.text.strip()
        if not owner or not repo or not token or len(session) < 12:
            self.log("Заполните GitHub owner/repo/token и общий ключ (минимум 12 символов).")
            return
        self.config = {
            "owner": owner, "repo": repo, "github_token": token,
            "session_key": session, "registry_file": REGISTRY_FILE
        }
        self.ensure_loop()
        self.active = True
        self.stop_requested = False
        if role == "Сервер":
            try:
                port = int(ids.port.text)
                host = ids.public_host.text.strip()
                if not 1 <= port <= 65535:
                    raise ValueError("Порт должен быть от 1 до 65535.")
                if not host or host in ("0.0.0.0", "127.0.0.1", "localhost"):
                    raise ValueError("Укажите внешний IP, DNS или VPN-адрес клиента.")
                self.config.update({
                    "server_id": ids.server_id.text.strip() or "nexus-" + self.install_id,
                    "server_name": ids.server_name.text.strip() or "NEXUS Server",
                    "public_host": host,
                    "listen_host": ids.listen_host.text.strip() or "0.0.0.0",
                    "port": port
                })
            except Exception as exc:
                self.active = False
                self.log(str(exc))
                return
            self.mode = "server"
            asyncio.run_coroutine_threadsafe(self.run_server(), self.loop)
        else:
            self.config["preferred_id"] = ids.preferred_id.text.strip()
            self.mode = "client"
            asyncio.run_coroutine_threadsafe(self.discovery_loop(), self.loop)

    def api_headers(self):
        return {
            "Authorization": "Bearer " + self.config["github_token"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

    def api_url(self):
        return (f"{GITHUB_API}/repos/{self.config['owner']}/"
                f"{self.config['repo']}/contents/{REGISTRY_FILE}")

    def github_read_registry_sync(self):
        response = requests.get(self.api_url(), headers=self.api_headers(), timeout=20)
        if response.status_code == 404:
            return {"version": 1, "servers": []}, None
        response.raise_for_status()
        payload = response.json()
        raw = base64.b64decode(payload["content"]).decode("utf-8")
        return json.loads(raw), payload.get("sha")

    def github_write_registry_sync(self, registry, sha, message):
        content = json.dumps(registry, ensure_ascii=False, indent=2)
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii")
        }
        if sha:
            body["sha"] = sha
        response = requests.put(self.api_url(), headers=self.api_headers(), json=body, timeout=20)
        response.raise_for_status()
        return response.json()

    async def registry_update(self, online):
        registry, sha = await asyncio.to_thread(self.github_read_registry_sync)
        servers = [s for s in registry.get("servers", [])
                   if s.get("id") != self.config["server_id"]]
        if online:
            now = datetime.now(timezone.utc)
            servers.append({
                "id": self.config["server_id"],
                "name": self.config["server_name"],
                "host": self.config["public_host"],
                "port": self.config["port"],
                "status": "online",
                "updated_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=REGISTRY_TTL_MINUTES)).isoformat()
            })
        new_registry = {"version": 1, "servers": servers}
        await asyncio.to_thread(
            self.github_write_registry_sync, new_registry, sha,
            ("NEXUS online: " if online else "NEXUS offline: ") + self.config["server_id"]
        )

    async def heartbeat_loop(self):
        while self.active and self.mode == "server":
            try:
                await self.registry_update(True)
                self.log("GitHub Registry обновлён.")
            except Exception as exc:
                self.log(f"GitHub heartbeat error: {exc}")
            await asyncio.sleep(PUBLISH_SECONDS)

    async def peer_handler(self, websocket):
        client_id = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=12)
            auth = json.loads(raw)
            if auth.get("type") != "auth" or auth.get("key") != self.config["session_key"]:
                await websocket.close(code=4001, reason="Unauthorized")
                return
            client_id = str(uuid.uuid4())[:8]
            self.peers[client_id] = websocket
            self.log(f"Клиент {client_id} подключён.")
            await websocket.send(json.dumps({"type": "welcome", "client_id": client_id}))
            async for raw in websocket:
                data = json.loads(raw)
                if data.get("type") == "message":
                    text = str(data.get("text", ""))[:2000]
                    self.log(f"{client_id}: {text}")
                    packet = json.dumps({"type": "message", "from": client_id, "text": text})
                    for other_id, peer in list(self.peers.items()):
                        if other_id != client_id:
                            try:
                                await peer.send(packet)
                            except Exception:
                                pass
        except Exception as exc:
            self.log(f"Соединение клиента завершено: {exc}")
        finally:
            if client_id:
                self.peers.pop(client_id, None)
                self.log(f"Клиент {client_id} отключён.")

    async def run_server(self):
        heartbeat = None
        try:
            self.server = await websockets.serve(
                self.peer_handler, self.config["listen_host"], self.config["port"],
                ping_interval=20, ping_timeout=20
            )
            self.set_status("SERVER ONLINE", True)
            self.log(f"Сервер слушает {self.config['listen_host']}:{self.config['port']}.")
            await self.registry_update(True)
            self.log("Сервер опубликован в GitHub Registry.")
            heartbeat = asyncio.create_task(self.heartbeat_loop())
            await self.server.wait_closed()
        except Exception as exc:
            self.log(f"Ошибка сервера: {exc}")
        finally:
            if heartbeat:
                heartbeat.cancel()
            for peer in list(self.peers.values()):
                try:
                    await peer.close()
                except Exception:
                    pass
            self.peers.clear()
            try:
                await self.registry_update(False)
                self.log("Запись сервера удалена из реестра.")
            except Exception as exc:
                self.log(f"Не удалось удалить запись: {exc}; она истечёт автоматически.")
            self.server = None
            self.active = False
            self.mode = None
            self.set_status("OFFLINE", False)

    async def read_active_servers(self):
        registry, _ = await asyncio.to_thread(self.github_read_registry_sync)
        now = datetime.now(timezone.utc)
        result = []
        for item in registry.get("servers", []):
            try:
                expiry = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
                if item.get("status") == "online" and expiry > now:
                    result.append(item)
            except Exception:
                continue
        return result

    async def discovery_loop(self):
        while self.active and self.mode == "client":
            try:
                servers = await self.read_active_servers()
                wanted = self.config.get("preferred_id", "")
                if wanted:
                    servers = [s for s in servers if s.get("id") == wanted]
                if not servers:
                    self.log("Активный сервер не найден. Следующая проверка через 5 минут.")
                else:
                    server = servers[0]
                    uri = f"ws://{server['host']}:{int(server['port'])}"
                    self.log(f"Найден {server.get('name', server['id'])}: {uri}")
                    await self.connect_once(uri)
                    if self.active:
                        self.log("Связь завершена. Повторный поиск через 5 минут.")
            except Exception as exc:
                self.log(f"Ошибка поиска через GitHub: {exc}")
            if self.active:
                await asyncio.sleep(DISCOVERY_SECONDS)
        self.set_status("OFFLINE", False)

    async def connect_once(self, uri):
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20, open_timeout=12) as ws:
                self.client_ws = ws
                await ws.send(json.dumps({"type": "auth", "key": self.config["session_key"]}))
                response = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if response.get("type") != "welcome":
                    raise RuntimeError("Сервер не подтвердил авторизацию.")
                self.set_status("CLIENT ONLINE", True)
                self.log(f"Подключено. Client ID: {response.get('client_id')}")
                async for raw in ws:
                    data = json.loads(raw)
                    if data.get("type") == "message":
                        self.log(f"{data.get('from')}: {data.get('text')}")
        except Exception as exc:
            self.log(f"Сервер недоступен: {exc}")
        finally:
            self.client_ws = None
            self.set_status("CLIENT WAITING", False)

    def send_message(self, text):
        text = text.strip()[:2000]
        if not text:
            return
        if self.mode == "client" and self.client_ws and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.client_ws.send(json.dumps({"type": "message", "text": text})), self.loop
            )
            self.log("Вы: " + text)
        elif self.mode == "server" and self.loop:
            async def broadcast():
                packet = json.dumps({"type": "message", "from": "SERVER", "text": text})
                for peer in list(self.peers.values()):
                    try:
                        await peer.send(packet)
                    except Exception:
                        pass
            asyncio.run_coroutine_threadsafe(broadcast(), self.loop)
            self.log("SERVER: " + text)
        else:
            self.log("Нет активного соединения.")

    def stop_action(self):
        if not self.active:
            self.log("Нет активного режима.")
            return
        self.active = False
        async def shutdown():
            if self.client_ws:
                try:
                    await self.client_ws.close()
                except Exception:
                    pass
            if self.server:
                self.server.close()
                await self.server.wait_closed()
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.set_status("STOPPING", False)
        self.log("Остановка запрошена; соединения закрываются.")

    def on_stop(self):
        self.stop_action()

if __name__ == "__main__":
    NexusApp().run()
