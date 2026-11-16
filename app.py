import io
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3
from flask import Flask, abort, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ["TABLE_NAME"]
BUCKET = os.environ["BUCKET_NAME"]

MAX_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES

table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=REGION)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _check_url(url):
    """Разрешены только http/https и только публичные IP-адреса (защита от SSRF)."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        abort(400, "Ссылка должна начинаться с http:// или https://")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        abort(400, "Не удалось определить адрес сайта по ссылке")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            abort(400, "Ссылка на внутренний адрес запрещена")


def fetch_image(url):
    """Скачивает картинку по ссылке: до 3 переходов, 10 МБ, только image/*."""
    for _ in range(4):
        _check_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "cloud-lab-app/1.0 (student lab)"})
        try:
            resp = _opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            abort(400, f"Сайт ответил ошибкой {e.code}")
        except Exception:
            abort(400, "Не удалось скачать картинку по ссылке")
        with resp:
            ctype = resp.headers.get_content_type()
            if ctype not in IMAGE_TYPES:
                abort(400, "По ссылке не картинка (jpg, png, webp или gif)")
            data = resp.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            abort(400, "Картинка больше 10 МБ")
        name = secure_filename(os.path.basename(urllib.parse.urlparse(url).path)) or "poster"
        if not name.lower().endswith(tuple(IMAGE_TYPES.values())):
            name += IMAGE_TYPES[ctype]
        return data, ctype, name
    abort(400, "Слишком много переадресаций")


@app.route("/")
def index():
    items = table.scan().get("Items", [])
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return render_template("index.html", items=items, host=socket.gethostname())


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    if not title:
        abort(400, "Название обязательно")

    item = {
        "id": str(uuid.uuid4()),
        "title": title,
        "description": request.form.get("description", "").strip(),
        "year": request.form.get("year", "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    f = request.files.get("file")
    poster_url = request.form.get("poster_url", "").strip()
    if f and f.filename:
        safe_name = secure_filename(f.filename) or "file"
        key = f"uploads/{item['id']}/{safe_name}"
        s3.upload_fileobj(
            f, BUCKET, key,
            ExtraArgs={"ContentType": f.content_type or "application/octet-stream"},
        )
        item["file_key"] = key
        item["file_name"] = f.filename
    elif poster_url:
        data, ctype, name = fetch_image(poster_url)
        key = f"uploads/{item['id']}/{name}"
        s3.upload_fileobj(io.BytesIO(data), BUCKET, key, ExtraArgs={"ContentType": ctype})
        item["file_key"] = key
        item["file_name"] = name
        item["source_url"] = poster_url

    table.put_item(Item=item)
    return redirect(url_for("index"))


@app.route("/file/<item_id>")
def get_file(item_id):
    item = table.get_item(Key={"id": item_id}).get("Item")
    if not item or "file_key" not in item:
        abort(404)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": item["file_key"]},
        ExpiresIn=300,
    )
    return redirect(url)


@app.route("/health")
def health():
    return {"status": "ok"}
