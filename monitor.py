"""
Опрос позиций ТС (FAW / AutoGRAPH Web) и уведомления в Telegram при приближении
к рамкам из KML в заданном радиусе.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import urllib.error
import urllib.parse
import urllib.request
import socket

from http_client import HttpSession

LOG = logging.getLogger("monitor")

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
EARTH_RADIUS_KM = 6371.0088

# Telegram-бот: токен и чат для sendMessage
TELEGRAM_BOT_TOKEN = "8738288447:AAF2AREBTrY0-lwzrpDD0x_pISC0zYIRs9Y"
TELEGRAM_CHAT_ID = "1093924638"
TELEGRAM_TIMEOUT_SEC = 20
TELEGRAM_SEND_RETRIES = 3
TELEGRAM_API_BASE = "https://api.telegram.org"
# Если в вашей сети Telegram режется, используйте relay (например Google Apps Script).
# Тогда monitor.py шлет в RELAY_URL, а relay уже отправляет в Telegram из облака.
TELEGRAM_RELAY_URL = "https://salairgeo-production.up.railway.app/notify"
TELEGRAM_RELAY_TOKEN = "325743759644957539677542117935587836943711256433688"

# KML с рамками в каталоге скрипта
FRAMES_KML_DEFAULT = Path(__file__).resolve().parent / "Рамки_просушка_1.kml"


def load_dotenv_file(path: Path) -> None:
    """
    Подставляет переменные из .env в os.environ (без внешних пакетов).
    Не перезаписывает уже заданные в системе/сессии ключи.
    Формат: KEY=value, комментарии #, UTF-8 с BOM.
    """
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value


@dataclass(frozen=True, slots=True)
class Frame:
    name: str
    lat: float
    lon: float


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по сфере (км)."""
    r1 = math.radians(lat1)
    r2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _polygon_centroid_deg(coords: list[tuple[float, float]]) -> tuple[float, float]:
    """Простой центроид в координатах градусов (достаточно для малых полигонов)."""
    if not coords:
        raise ValueError("empty coordinates")
    n = len(coords)
    slat = sum(c[1] for c in coords)
    slon = sum(c[0] for c in coords)
    return slat / n, slon / n


def _parse_kml_coordinates(text: str) -> list[tuple[float, float]]:
    """KML: lon,lat,alt через пробелы."""
    out: list[tuple[float, float]] = []
    for triple in text.split():
        parts = triple.split(",")
        if len(parts) < 2:
            continue
        lon = float(parts[0])
        lat = float(parts[1])
        out.append((lon, lat))
    return out


def load_frames_kml(path: Path) -> list[Frame]:
    tree = ET.parse(path)
    root = tree.getroot()
    frames: list[Frame] = []
    for pm in root.findall(".//k:Placemark", KML_NS):
        name_el = pm.find("k:name", KML_NS)
        poly = pm.find(".//k:Polygon/k:outerBoundaryIs/k:LinearRing/k:coordinates", KML_NS)
        if name_el is None or poly is None or not (poly.text and poly.text.strip()):
            continue
        raw = poly.text.strip()
        pts = _parse_kml_coordinates(raw)
        if len(pts) < 3:
            continue
        lat, lon = _polygon_centroid_deg(pts)
        frames.append(Frame(name=name_el.text.strip(), lat=lat, lon=lon))
    if not frames:
        raise ValueError(f"no polygon placemarks in {path}")
    LOG.info("loaded %d frames from %s", len(frames), path)
    return frames


def parse_faw_embedded_config(html: str) -> tuple[int, list[int]]:
    org_m = re.search(r"currentOrgID:\s*(\d+)", html)
    if not org_m:
        raise ValueError("currentOrgID not found in FAW page (layout changed?)")
    org_id = int(org_m.group(1))
    ids_m = re.search(r'"ElementIDs":\s*\[([^\]]*)\]', html)
    if not ids_m:
        raise ValueError("ElementIDs not found in FAW page")
    raw = ids_m.group(1).strip()
    if not raw:
        raise ValueError("ElementIDs empty")
    car_ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    return org_id, car_ids


class FawSession:
    def __init__(self, tracking_url: str, http: HttpSession) -> None:
        self.tracking_url = tracking_url
        self.http = http
        self._org_id: int | None = None
        self._car_ids: list[int] | None = None

    def bootstrap(self) -> None:
        html = self.http.get(self.tracking_url, timeout=45)
        self._org_id, self._car_ids = parse_faw_embedded_config(html)
        LOG.info("FAW org_id=%s cars=%d", self._org_id, len(self._car_ids or ()))

    def fetch_positions(self) -> list[dict]:
        if self._org_id is None or not self._car_ids:
            raise RuntimeError("bootstrap() first")
        base = f"{urlparse(self.tracking_url).scheme}://{urlparse(self.tracking_url).netloc}"
        url = base.rstrip("/") + "/Track/Positions"
        idcars = ",".join(str(i) for i in self._car_ids)
        data = {
            "id": str(self._org_id),
            "type": "0",
            "idc": str(self._org_id),
            "idgeo": "-1",
            "gtype": "-1",
            "idcars": idcars,
            "virtualTreeId": "",
        }
        raw = self.http.post_form(
            url,
            data,
            extra_headers={"Referer": self.tracking_url},
            timeout=45,
        )
        payload = json.loads(raw)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("unexpected Track/Positions JSON")
        return items


def vehicle_label(item: dict) -> str:
    props = item.get("Props")
    if isinstance(props, list) and props and isinstance(props[0], str) and props[0].strip():
        return props[0].strip()
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return str(item.get("id", "?"))


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """
    Отправка уведомления в Telegram отдельным каналом:
    - не использует FAW-сессию;
    - не таскает FAW cookies/headers;
    - игнорирует системные proxy для Telegram (частая причина timeout в Windows).
    """
    u = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    telegram_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    for attempt in range(1, TELEGRAM_SEND_RETRIES + 1):
        try:
            # 1) JSON body
            body_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                u,
                data=body_json,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "Mozilla/5.0 (compatible; TelegramNotifier/1.0)",
                },
                method="POST",
            )
            with telegram_opener.open(req, timeout=TELEGRAM_TIMEOUT_SEC) as resp:
                resp_text = resp.read().decode("utf-8", errors="replace")
                if resp.getcode() == 200:
                    return True
                LOG.error(
                    "Telegram HTTP %s (attempt %d/%d): %s",
                    resp.getcode(),
                    attempt,
                    TELEGRAM_SEND_RETRIES,
                    resp_text[:500],
                )
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(e)
            LOG.error(
                "Telegram HTTPError %s (attempt %d/%d): %s",
                getattr(e, "code", "?"),
                attempt,
                TELEGRAM_SEND_RETRIES,
                body[:500],
            )
        except (urllib.error.URLError, TimeoutError, OSError, socket.timeout) as e:
            # 2) fallback: x-www-form-urlencoded (иногда проходит там, где json режется)
            try:
                form = urllib.parse.urlencode(payload).encode("utf-8")
                req2 = urllib.request.Request(
                    u,
                    data=form,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0 (compatible; TelegramNotifier/1.0)",
                    },
                    method="POST",
                )
                with telegram_opener.open(req2, timeout=TELEGRAM_TIMEOUT_SEC) as resp2:
                    resp_text2 = resp2.read().decode("utf-8", errors="replace")
                    if resp2.getcode() == 200:
                        return True
                    LOG.error(
                        "Telegram form HTTP %s (attempt %d/%d): %s",
                        resp2.getcode(),
                        attempt,
                        TELEGRAM_SEND_RETRIES,
                        resp_text2[:500],
                    )
            except Exception:
                LOG.error(
                    "Telegram network error (attempt %d/%d): %s",
                    attempt,
                    TELEGRAM_SEND_RETRIES,
                    e,
                )
        if attempt < TELEGRAM_SEND_RETRIES:
            # Плавный backoff: 2с, 4с...
            time.sleep(2 ** attempt)
    return False


def send_telegram_via_relay(text: str) -> bool:
    """Отправка в облачный relay endpoint (Google Apps Script / Cloud Function)."""
    if not TELEGRAM_RELAY_URL.strip():
        return False

    payload = {
        "text": text,
        "chat_id": TELEGRAM_CHAT_ID,
        "relay_token": TELEGRAM_RELAY_TOKEN,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        TELEGRAM_RELAY_URL.strip(),
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Mozilla/5.0 (compatible; TelegramRelayClient/1.0)",
        },
        method="POST",
    )
    try:
        # relay обычно доступен из сети, где Telegram напрямую недоступен
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_SEC) as resp:
            resp_text = resp.read().decode("utf-8", errors="replace")
            if resp.getcode() == 200:
                return True
            LOG.error("Relay HTTP %s: %s", resp.getcode(), resp_text[:500])
            return False
    except Exception as e:
        LOG.error("Relay send failed: %s", e)
        return False


def _configure_logging() -> None:
    """
    Настраивает вывод в stderr. Используем force=True (3.8+), иначе в IDE/Cursor
    уже настроенный root-logger оставляет basicConfig без эффекта — INFO не видно.
    """
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if sys.version_info >= (3, 8):
        logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr, force=True)
    else:
        root = logging.getLogger()
        root.handlers.clear()
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(fmt))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    # Дочерний логгер «monitor» должен отдавать записи на root
    LOG.setLevel(logging.INFO)
    LOG.propagate = True


def run() -> None:
    base_dir = Path(__file__).resolve().parent
    load_dotenv_file(base_dir / ".env")

    _configure_logging()
    print("FAW monitor: старт, загрузка KML и подключение к серверу…", flush=True)

    token = TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    tracking_url = os.environ.get(
        "FAW_TRACKING_URL",
        "https://web.faw.proffit.ru/Strict/Token/dff4e02b-7512-43af-a1c3-a90451c17b41",
    ).strip()

    kml_path = Path(os.environ.get("FRAMES_KML_PATH", str(FRAMES_KML_DEFAULT))).expanduser()
    poll_sec = float(os.environ.get("POLL_INTERVAL_SEC", "30"))
    radius_km = float(os.environ.get("ALERT_RADIUS_KM", "5"))

    if not kml_path.is_file():
        LOG.error(
            "KML не найден: %s — положите файл или задайте FRAMES_KML_PATH (полный путь к .kml)",
            kml_path,
        )
        sys.exit(2)

    frames = load_frames_kml(kml_path)
    http = HttpSession()

    faw = FawSession(tracking_url, http)
    faw.bootstrap()

    # Пара (id ТС, имя рамки) — уже в зоне; уведомляем только при входе
    inside: set[tuple[int, str]] = set()

    LOG.info("radius=%s km, interval=%s s, frames=%d", radius_km, poll_sec, len(frames))
    print(
        f"Опрос каждые {poll_sec} с; уведомления только при въезде в радиус {radius_km} км. Ctrl+C — выход.",
        flush=True,
    )

    poll_n = 0
    while True:
        loop_t0 = time.monotonic()
        try:
            items = faw.fetch_positions()
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, TimeoutError, OSError) as e:
            LOG.warning("FAW poll failed: %s", e)
            time.sleep(min(poll_sec, 60.0))
            continue

        new_inside: set[tuple[int, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            lat = item.get("Lat")
            lng = item.get("Lng")
            if not isinstance(cid, int) or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            vlat, vlng = float(lat), float(lng)
            label = vehicle_label(item)
            for fr in frames:
                d = _haversine_km(vlat, vlng, fr.lat, fr.lon)
                if d <= radius_km:
                    new_inside.add((cid, fr.name))

        entered = new_inside - inside
        for cid, fr_name in entered:
            label = next(
                (vehicle_label(it) for it in items if isinstance(it, dict) and it.get("id") == cid),
                str(cid),
            )
            msg = f"ТС {label} приближается к рамке {fr_name}"
            sent_ok = False
            try:
                sent_ok = send_telegram_via_relay(msg) or send_telegram(token, chat_id, msg)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                LOG.error("telegram failed for '%s': %s", msg, e)
            if sent_ok:
                LOG.info("notify: %s", msg)
            else:
                LOG.error("telegram send not delivered: %s", msg)

        inside = new_inside

        poll_n += 1
        LOG.info(
            "опрос #%d: ТС в ответе=%d, пар «в зоне»=%d (уведомление только при новом въезде)",
            poll_n,
            len(items),
            len(new_inside),
        )

        elapsed = time.monotonic() - loop_t0
        sleep_for = max(0.0, poll_sec - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
