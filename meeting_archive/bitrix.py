from __future__ import annotations

import asyncio
import ipaddress
import time
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlsplit, urlencode

import httpx

from .settings import Settings, Vault, portal_domain

TOKEN_URL = "https://oauth.bitrix.info/oauth/token/"
METADATA_FIELDS = ["callId", "uuid", "callType", "chatId", "initiatorId", "startDate", "endDate", "durationSeconds",
                   "participants", "outcomes", "createdAt", "version", "overview.topic", "tracks"]


def oauth_scopes(value) -> set[str]:
    if isinstance(value, str):
        return {part.strip() for part in unquote(value).split(",") if part.strip()}
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return {part.strip() for part in value if part.strip()}
    return set()


class BitrixError(RuntimeError):
    def __init__(self, message: str, *, auth=False, retryable=False, code="", retry_at=0):
        super().__init__(message)
        self.auth, self.retryable = auth, retryable
        self.code = code.upper()
        self.retry_at = retry_at


class BitrixClient:
    def __init__(self, settings: Settings, vault: Vault, transport=None):
        self.settings, self.vault = settings, vault
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), transport=transport, follow_redirects=False)
        self.refresh_lock = asyncio.Lock()
        # A single gate is shared by meetings, chats and copied file clients.
        # Stay below the ordinary cloud's sustainable 2 HTTP requests/second.
        self.request_lock = asyncio.Lock()
        self.request_budget = {"next_at": 0.0, "blocked_until": 0.0, "failures": 0}
        self.method_budget = {}
        self.request_interval = 1.0
        self.metrics = {}

    async def measured_post(self, method, url, payload):
        metric = self.metrics.setdefault(method, {"requests": 0, "seconds": 0.0, "errors": 0, "last_seconds": 0.0})
        start = time.monotonic()
        metric["requests"] += 1
        try:
            return await self.http.post(url, json=payload)
        except httpx.HTTPError:
            metric["errors"] += 1
            raise
        finally:
            elapsed = time.monotonic() - start
            metric["seconds"] += elapsed
            metric["last_seconds"] = elapsed

    async def batch_pages(self, commands):
        """Independent legacy REST calls, with a budget and outcome for each method."""
        if not 1 <= len(commands) <= 50:
            raise ValueError("Пакет должен содержать от 1 до 50 команд")
        outcomes, pending = {}, {}
        for key, (method, params) in commands.items():
            budget = self.method_budget.get(method, {})
            if budget.get("until", 0) > time.time():
                outcomes[key] = BitrixError("Ожидается безопасный повтор метода", code="RATE_LIMITED",
                                           retryable=True, retry_at=budget["until"])
            else:
                pending[key] = method + '?' + urlencode(params, doseq=True)
        if not pending:
            return outcomes
        result = await self.call("batch", {"halt": 0, "cmd": pending}, v3=False)
        if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
            raise BitrixError("Неизвестный формат пакета сообщений")
        times = result.get("result_time") or {}
        errors = result.get("result_error") or {}
        for key in pending:
            method = commands[key][0]
            metric = self.metrics.setdefault(method, {"requests": 0, "seconds": 0.0, "errors": 0, "last_seconds": 0.0})
            metric["subrequests"] = metric.get("subrequests", 0) + 1
            budget = self.method_budget.setdefault(method, {"until": 0.0, "reset_at": 0.0})
            timing = times.get(key) or {}
            try:
                reset, operating = float(timing.get("operating_reset_at", 0)), float(timing.get("operating", 0))
            except (ValueError, TypeError, AttributeError):
                reset, operating = 0, 0
            budget["reset_at"] = max(budget.get("reset_at", 0), reset)
            if operating >= 240 and reset > time.time():
                budget["until"] = max(budget.get("until", 0), reset)
            if key in errors:
                error = errors[key]
                code = str(error.get("error") or "BATCH_ERROR").upper()
                limited = code in {"OPERATION_TIME_LIMIT", "QUERY_LIMIT_EXCEEDED", "RATE_LIMITED"}
                if limited:
                    budget["until"] = max(budget.get("until", 0), time.time() + 60)
                metric["errors"] += 1
                # Descriptions can include source content: retain only the vendor code.
                outcomes[key] = BitrixError("Bitrix24: " + code, code=code, retryable=limited,
                                           retry_at=budget.get("until", 0) if limited else 0)
            elif key in result["result"]:
                outcomes[key] = result["result"][key]
            else:
                outcomes[key] = BitrixError("В пакете отсутствует результат команды", retryable=True)
        return outcomes

    async def close(self):
        await self.http.aclose()

    def validate_tokens(self, data: dict, *, member_id: str = "") -> dict:
        if not all(isinstance(data.get(key), str) and data[key] for key in ("access_token", "refresh_token", "member_id")):
            raise BitrixError("Bitrix24 не вернул полную пару токенов OAuth", auth=True)
        scopes = oauth_scopes(data.get("scope"))
        if scopes and "call" not in scopes and "app" not in scopes:
            raise BitrixError("Добавьте право call в приложении Bitrix24 и повторите вход", auth=True)
        endpoint = urlsplit(data.get("client_endpoint") or "")
        if (endpoint.scheme != "https" or endpoint.hostname != self.settings.portal
            or endpoint.port not in (None, 443) or endpoint.username or endpoint.password
            or endpoint.path != "/rest/" or endpoint.query or endpoint.fragment):
            raise BitrixError("OAuth вернул небезопасный адрес портала. Подключение остановлено", auth=True)
        if member_id and data["member_id"] != member_id:
            raise BitrixError("При обновлении OAuth изменился идентификатор портала", auth=True)
        data["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        return data

    async def confirm_token_permissions(self, data: dict) -> dict:
        if "call" in oauth_scopes(data.get("scope")):
            return data
        # Local OAuth may return the token-service scope "app", rather than the
        # application's REST grants. Ask the portal using this candidate token;
        # never request full=True, which would list all portal capabilities.
        portal = portal_domain(self.settings.portal)
        response = await self.http.post(f"https://{portal}/rest/scope", json={"auth": data["access_token"]})
        granted = self._response(response).get("result")
        if not isinstance(granted, list) or not all(isinstance(value, str) for value in granted):
            raise BitrixError("Bitrix24 не подтвердил список прав подключения. Повторите вход", auth=True)
        scopes = oauth_scopes(granted)
        if "call" not in scopes:
            raise BitrixError("Bitrix24 не предоставил этому подключению право call. Сохраните права приложения и повторите вход", auth=True)
        data["scope"] = ",".join(sorted(scopes))
        return data

    async def exchange(self, code: str, client_id: str, client_secret: str, redirect_uri: str = "") -> dict:
        parameters = {"grant_type": "authorization_code", "client_id": client_id,
                      "client_secret": client_secret, "code": code}
        if redirect_uri:
            parameters["redirect_uri"] = redirect_uri
        response = await self.http.post(TOKEN_URL, data=parameters)
        return await self.confirm_token_permissions(self.validate_tokens(self._response(response)))

    @staticmethod
    def _response(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            if response.status_code == 429:
                raise BitrixError("Ограничение частоты Bitrix24. Повторим позже", retryable=True, code="RATE_LIMITED") from exc
            raise BitrixError("Bitrix24 вернул ответ без JSON", retryable=response.status_code >= 500) from exc
        if not isinstance(data, dict):
            raise BitrixError("Bitrix24 вернул неизвестный формат JSON. Ответ не считается пустым",
                              auth=response.status_code in (401, 403), retryable=response.status_code >= 500)
        if data.get("error"):
            raw = data["error"]
            code = str(raw.get("code", "ERROR") if isinstance(raw, dict) else raw)
            auth = code.upper() in {"INVALID_TOKEN", "EXPIRED_TOKEN", "INVALID_CLIENT", "INVALID_CREDENTIALS", "NO_AUTH_FOUND", "INSUFFICIENT_SCOPE", "WRONG_AUTH_TYPE",
                "ACCESS_DENIED", "BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION", "BITRIX_REST_V3_EXCEPTION_INSUFFICIENTSCOPEEXCEPTION"}
            retry = code.upper() in {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "RATE_LIMITED", "INTERNAL_SERVER_ERROR"}
            if code.upper() in {"ACCESS_DENIED", "BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION"}:
                raise BitrixError(f"Bitrix24 запретил доступ к материалам совещаний ({code}). Проверьте пользователя подключения; после изменения прав сохраните приложение и выполните новый вход", auth=True, code=code)
            if code.upper() == "INVALID_CREDENTIALS":
                raise BitrixError(f"Подключение Bitrix24 недействительно или отозвано ({code}). Выполните новый вход либо замените webhook", auth=True)
            if code.upper() == "BITRIX_REST_V3_EXCEPTION_INSUFFICIENTSCOPEEXCEPTION":
                raise BitrixError(f"Bitrix24 не предоставил право call этому подключению ({code}). Сохраните права приложения и выполните новый вход", auth=True)
            raise BitrixError(f"Bitrix24: {code}. Проверьте доступ и права приложения" if auth else f"Bitrix24: {code}", auth=auth, retryable=retry, code=code)
        if response.status_code >= 400:
            raise BitrixError(f"Bitrix24: HTTP {response.status_code}", auth=response.status_code in (401, 403), retryable=response.status_code >= 500 or response.status_code == 429, code="RATE_LIMITED" if response.status_code == 429 else "")
        return data

    async def token(self, force=False, failed_token: str = "") -> str:
        async with self.refresh_lock:
            secrets = self.vault.read()
            if not secrets.get("access_token"):
                raise BitrixError("Выполните вход в Bitrix24", auth=True)
            # Bitrix24 renews in response to expired_token. The lock and old-token
            # guard prevent concurrent requests from rotating the same pair twice.
            expiring = bool(secrets.get("expires_at")) and float(secrets["expires_at"]) <= time.time() + 60
            if expiring or (force and (not failed_token or secrets["access_token"] == failed_token)):
                result = self._response(await self.http.post(TOKEN_URL, data={"grant_type": "refresh_token",
                    "client_id": secrets.get("client_id", ""), "client_secret": secrets.get("client_secret", ""),
                    "refresh_token": secrets.get("refresh_token", "")}))
                result = self.validate_tokens(result, member_id=self.settings.member_id)
                result = await self.confirm_token_permissions(result)
                self.vault.update(access_token=result["access_token"], refresh_token=result["refresh_token"],
                                  expires_at=result["expires_at"])
                secrets = self.vault.read()
            return secrets["access_token"]

    @staticmethod
    def retry_after(response):
        value = response.headers.get("Retry-After", "")
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                return 0.0

    async def rest_request(self, method, url, payload):
        """One paced HTTP request; long cooldowns return to the durable queue."""
        async with self.request_lock:
            budget = self.method_budget.setdefault(method, {"until": 0.0, "reset_at": 0.0})
            until = max(budget["until"], self.request_budget["blocked_until"])
            if until > time.time():
                raise BitrixError("Bitrix24: ожидается безопасный повтор после ограничения нагрузки", retryable=True,
                                  code="RATE_LIMITED", retry_at=until)
            delay = max(0.0, self.request_budget["next_at"] - time.monotonic())
            if delay:
                await asyncio.sleep(delay)
                until = max(budget["until"], self.request_budget["blocked_until"])
                if until > time.time():
                    raise BitrixError("Bitrix24: ожидается безопасный повтор после ограничения нагрузки", retryable=True,
                                      code="RATE_LIMITED", retry_at=until)
            self.request_budget["next_at"] = time.monotonic() + self.request_interval
        # Pace starts without holding the gate through network latency: this
        # preserves concurrent OAuth refresh guards and fair request admission.
        response = await self.measured_post(method, url, payload)
        try:
            data = self._response(response)
        except BitrixError as exc:
            self.metrics[method]["errors"] += 1
            if exc.code in {"QUERY_LIMIT_EXCEEDED", "RATE_LIMITED", "OPERATION_TIME_LIMIT"}:
                if exc.code == "OPERATION_TIME_LIMIT":
                    budget["until"] = max(time.time() + max(60, self.retry_after(response)), budget["reset_at"])
                    exc.retry_at = budget["until"]
                else:
                    self.request_budget["failures"] += 1
                    seconds = max(self.retry_after(response), min(60, 2 ** min(6, self.request_budget["failures"])))
                    self.request_budget["blocked_until"] = time.time() + seconds
                    exc.retry_at = self.request_budget["blocked_until"]
            raise
        self.request_budget["failures"] = 0
        metrics = data.get("time") or {}
        if isinstance(metrics, dict):
            try:
                reset = float(metrics.get("operating_reset_at", 0))
                operating = float(metrics.get("operating", 0))
            except (TypeError, ValueError):
                reset, operating = 0, 0
            budget["reset_at"] = reset
            # This is our conservative soft budget, not a claimed vendor
            # threshold (the actual cloud threshold is configurable).
            budget["until"] = reset if operating >= 240 and reset > time.time() else 0.0
        return data

    async def call(self, method: str, params: dict, *, v3=True) -> dict:
        portal = portal_domain(self.settings.portal)
        if self.settings.auth_mode == "webhook":
            hook = self.vault.read().get("webhook", "")
            parsed = urlsplit(hook)
            if parsed.hostname != portal or parsed.scheme != "https" or parsed.query or parsed.fragment:
                raise BitrixError("Проверьте адрес входящего webhook", auth=True)
            tail = parsed.path.removeprefix("/rest/").removeprefix("api/").strip("/")
            if len(tail.split("/")) != 2:
                raise BitrixError("Укажите базовый адрес webhook без имени метода", auth=True)
            url = f"https://{portal}/rest/{'api/' if v3 else ''}{tail}/{method}"
            payload = params
        else:
            url = f"https://{portal}/rest/{'api/' if v3 else ''}{method}"
            payload = {**params, "auth": await self.token()}
        for attempt in range(2):
            try:
                data = await self.rest_request(method, url, payload)
                return data.get("result", data)
            except BitrixError as exc:
                recoverable = exc.code in {"EXPIRED_TOKEN", "INVALID_TOKEN", "NO_AUTH_FOUND"} or (
                    v3 and method.startswith("call.followup.") and
                    exc.code == "BITRIX_REST_V3_EXCEPTION_ACCESSDENIEDEXCEPTION")
                if attempt == 0 and exc.auth and self.settings.auth_mode == "oauth" and recoverable:
                    payload["auth"] = await self.token(force=True, failed_token=payload["auth"])
                    continue
                raise
        raise BitrixError("Не удалось вызвать Bitrix24")

    async def profile(self) -> dict:
        return await self.call("user.current", {}, v3=False)

    async def chat_title(self, chat_id: int) -> str:
        result = await self.call("im.dialog.get", {"DIALOG_ID": f"chat{int(chat_id)}"}, v3=False)
        if not isinstance(result, dict) or int(result.get("id", 0)) != int(chat_id):
            raise BitrixError("Bitrix24 вернул данные другого чата")
        return str(result.get("name") or "").strip()

    async def chat_titles(self, chat_ids):
        ids = sorted({int(i) for i in chat_ids if int(i) > 0})
        if len(ids) > 50:
            raise ValueError("Не более 50 чатов в одном пакете")
        if not ids:
            return {}, {}
        result = await self.call("batch", {"halt": 0, "cmd": {f"chat_{i}": f"im.dialog.get?DIALOG_ID=chat{i}" for i in ids}}, v3=False)
        if not isinstance(result, dict):
            raise BitrixError("Неизвестный формат пакета названий чатов")
        titles, errors = {}, result.get("result_error") or {}
        replies = result.get("result") or {}
        for chat_id in ids:
            data = replies.get(f"chat_{chat_id}", {}) if isinstance(replies, dict) else {}
            if isinstance(data, dict) and str(data.get("id")) == str(chat_id) and data.get("name"):
                titles[chat_id] = str(data["name"]).strip()
        return titles, errors

    async def personal_chat_titles(self, peers, names=None):
        """Resolve a candidate personal dialog only if its internal chat ID matches."""
        if not peers:
            return {}
        if len(peers) > 50:
            raise ValueError("Не более 50 диалогов в одном пакете")
        result = await self.call("batch", {"halt": 0, "cmd": {
            f"personal_{int(chat_id)}": f"im.dialog.get?DIALOG_ID={int(user_id)}"
            for chat_id, user_id in peers.items()}}, v3=False)
        replies = result.get("result", {}) if isinstance(result, dict) else {}
        titles = {}
        for chat_id in peers:
            data = replies.get(f"personal_{int(chat_id)}", {}) if isinstance(replies, dict) else {}
            if not isinstance(data, dict) or str(data.get("id")) != str(chat_id):
                continue
            name = str(data.get("name") or "").strip()
            if not name and data.get("type") == "private":
                name = (names or {}).get(peers[chat_id], "")
            if name:
                titles[int(chat_id)] = "Личный диалог: " + name
        return titles

    async def catalogue(self, start: str, end: str):
        cursor = None
        seen = set()
        while True:
            pagination = {"limit": 20}
            if cursor is not None:
                pagination["afterCursor"] = cursor
            result = await self.call("call.followup.list", {"filter": {"startDate": {"from": start, "to": end},
                "participantId": self.settings.user_id}, "select": METADATA_FIELDS,
                "order": {"startDate": "asc"}, "pagination": pagination, "mentionFormat": "none"})
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise BitrixError("Неизвестный формат REST 3.0. Каталог не считается пустым")
            yield result["items"]
            if not result.get("hasMore"):
                break
            cursor = result.get("afterCursor")
            signature = repr(cursor)
            if cursor is None or signature in seen:
                raise BitrixError("Bitrix24 вернул повторяющийся курсор каталога")
            seen.add(signature)
            await asyncio.sleep(0.2)

    async def recent_dialogs(self, offset=0) -> dict:
        result = await self.call("im.recent.list", {"OFFSET": offset, "LIMIT": 50,
            "SKIP_OPENLINES": "Y", "PARSE_TEXT": "N", "GET_ORIGINAL_TEXT": "N"}, v3=False)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise BitrixError("Неизвестный формат списка чатов")
        return result

    async def dialog_messages(self, dialog_id: str, chat_id: int, before=0) -> list[dict]:
        params = {"DIALOG_ID": dialog_id, "LIMIT": 50}
        if before:
            params["LAST_ID"] = before
        result = await self.call("im.dialog.messages.get", params, v3=False)
        if (not isinstance(result, dict) or str(result.get("chat_id")) != str(chat_id)
                or not isinstance(result.get("messages"), list)):
            raise BitrixError("Bitrix24 вернул историю другого чата или неизвестный формат сообщений")
        return result["messages"]

    async def followup_metadata(self, call_id: str) -> dict:
        return await self._followup(call_id, METADATA_FIELDS)

    async def followup(self, call_id: str) -> dict:
        return await self._followup(call_id, METADATA_FIELDS + ["transcription"])

    async def _followup(self, call_id: str, fields: list[str]) -> dict:
        result = await self.call("call.followup.get", {"callId": int(call_id), "select": fields, "mentionFormat": "none"})
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise BitrixError("Неизвестный формат ответа Follow-up")
        return result["item"]

    def safe_download_url(self, value: str) -> str:
        url = urljoin("https://" + self.settings.portal + "/", value)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise BitrixError("Небезопасный адрес записи. Скачивание остановлено")
        try:
            ipaddress.ip_address(host)
            raise BitrixError("Адрес записи содержит IP вместо домена")
        except ValueError:
            pass
        allowed = host == self.settings.portal or any(host.endswith("." + suffix) for suffix in
            ("bitrix24.ru", "bitrix24.com", "bitrix24.net", "bitrix24.tech", "bitrix.info", "bx24.net", "bitrix24-cdn.com"))
        if not allowed:
            raise BitrixError("Неизвестный домен хранения записи. Используйте локальный импорт; домен требует проверки")
        return url

    async def download_url(self, track: dict) -> str:
        if track.get("diskFileId"):
            try:
                data = await self.call("disk.file.get", {"id": track["diskFileId"]}, v3=False)
                if data.get("DOWNLOAD_URL"):
                    return self.safe_download_url(data["DOWNLOAD_URL"])
            except BitrixError:
                pass  # call scope may expose tracks without disk scope.
        value = track.get("url") or track.get("relUrl")
        if not value:
            raise BitrixError("Для записи пока нет адреса скачивания", retryable=True)
        return self.safe_download_url(value)
