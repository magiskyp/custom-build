import argparse
import datetime as dt
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def as_text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def slugify(value, fallback="resource", max_length=80):
    cleaned = SAFE_NAME_RE.sub("-", as_text(value)).strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length]


def guess_suffix(url="", content_type=""):
    lowered_url = as_text(url).lower()
    lowered_type = as_text(content_type).lower()
    if ".js" in lowered_url or "javascript" in lowered_type:
        return ".js"
    if ".css" in lowered_url or "text/css" in lowered_type:
        return ".css"
    if ".html" in lowered_url or ".htm" in lowered_url or "text/html" in lowered_type:
        return ".html"
    return ".txt"


def write_text(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(as_text(content), encoding="utf-8")


def build_resource_name(index, url="", content_type=""):
    parsed = urlparse(as_text(url))
    raw_name = Path(parsed.path).name if parsed.path else ""
    stem = Path(raw_name).stem if raw_name else ""
    suffix = guess_suffix(url, content_type)
    return f"{index:03d}-{slugify(stem, fallback='resource')}{suffix}"


def get_documents(payload):
    documents = payload.get("documents")
    if isinstance(documents, list) and documents:
        return documents

    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    return [
        {
            "framePath": "root",
            "href": location.get("href") or "",
            "title": payload.get("title") or "",
            "html": payload.get("html") or "",
            "scripts": payload.get("scripts") or [],
            "modulePreloads": payload.get("modulePreloads") or [],
            "stylesheets": payload.get("stylesheets") or [],
            "accessible": True,
        }
    ]


def save_documents(output_dir: Path, base_name: str, payload):
    documents = get_documents(payload)
    documents_dir = output_dir / f"{base_name}-documents"
    inline_dir = output_dir / f"{base_name}-inline"
    manifest_path = output_dir / f"{base_name}-documents-manifest.json"
    saved_documents = 0
    saved_inline = 0
    manifest = []

    for doc_index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            continue

        frame_path = as_text(document.get("framePath") or f"document-{doc_index}")
        doc_prefix = f"{doc_index:02d}-{slugify(frame_path, fallback=f'document-{doc_index:02d}')}"
        html = as_text(document.get("html"))

        if html:
            write_text(documents_dir / f"{doc_prefix}.html", html)
            saved_documents += 1

        scripts = document.get("scripts") if isinstance(document.get("scripts"), list) else []
        stylesheets = document.get("stylesheets") if isinstance(document.get("stylesheets"), list) else []

        for script_index, script in enumerate(scripts, start=1):
            if not isinstance(script, dict) or not script.get("inline"):
                continue
            content = as_text(script.get("content"))
            if not content:
                continue
            write_text(inline_dir / f"{doc_prefix}-script-{script_index:03d}.js", content)
            saved_inline += 1

        for style_index, stylesheet in enumerate(stylesheets, start=1):
            if not isinstance(stylesheet, dict) or not stylesheet.get("inline"):
                continue
            content = as_text(stylesheet.get("content"))
            if not content:
                continue
            write_text(inline_dir / f"{doc_prefix}-style-{style_index:03d}.css", content)
            saved_inline += 1

        manifest.append(
            {
                "framePath": frame_path,
                "href": as_text(document.get("href")),
                "title": as_text(document.get("title")),
                "accessible": bool(document.get("accessible", True)),
                "error": as_text(document.get("error")),
                "savedHtml": bool(html),
                "scripts": len(scripts),
                "stylesheets": len(stylesheets),
                "modulePreloads": len(document.get("modulePreloads") or []),
            }
        )

    write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return {
        "documents_dir": documents_dir if saved_documents else None,
        "documents_count": saved_documents,
        "inline_dir": inline_dir if saved_inline else None,
        "inline_count": saved_inline,
        "manifest_path": manifest_path,
    }


def save_fetched_resources(output_dir: Path, base_name: str, payload):
    fetched_resources = payload.get("fetchedResources") if isinstance(payload.get("fetchedResources"), list) else []
    resources_dir = output_dir / f"{base_name}-resources"
    manifest_path = output_dir / f"{base_name}-resources-manifest.json"
    resources_saved = 0
    manifest = []

    for index, resource in enumerate(fetched_resources, start=1):
        if not isinstance(resource, dict):
            continue

        content = as_text(resource.get("content"))
        if not content:
            continue

        url = as_text(resource.get("url"))
        content_type = as_text(resource.get("contentType"))
        resource_name = build_resource_name(index, url, content_type)
        write_text(resources_dir / resource_name, content)
        resources_saved += 1

        manifest.append(
            {
                "index": index,
                "url": url,
                "status": resource.get("status"),
                "ok": bool(resource.get("ok")),
                "contentType": content_type,
                "sources": resource.get("sources") or [],
                "savedAs": resource_name,
            }
        )

    if manifest:
        write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "resources_dir": resources_dir if resources_saved else None,
        "resources_count": resources_saved,
        "manifest_path": manifest_path if manifest else None,
    }


class DumpHandler(BaseHTTPRequestHandler):
    server_version = "WebviewDumpServer/1.0"

    def _send_headers(self, status_code=200, content_type="application/json; charset=utf-8"):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} - {fmt % args}")

    def do_OPTIONS(self):
        self._send_headers(204)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health", "/healthz"):
            payload = {
                "ok": True,
                "message": "webview dump server is running",
                "output_dir": str(self.server.output_dir),
            }
            self._send_headers(200)
            self.wfile.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            return
        self._send_headers(404)
        self.wfile.write(json.dumps({"ok": False, "error": "not found"}).encode("utf-8"))

    def do_POST(self):
        if self.path.rstrip("/") != "/upload":
            self._send_headers(404)
            self.wfile.write(json.dumps({"ok": False, "error": "not found"}).encode("utf-8"))
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": "empty body"}).encode("utf-8"))
            return

        raw_body = self.rfile.read(content_length)
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base_name = f"webview-dump-{timestamp}"
        json_path = self.server.output_dir / f"{base_name}.json"
        html_path = self.server.output_dir / f"{base_name}.html"

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as error:
            self._send_headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": f"invalid json: {error}"}).encode("utf-8"))
            return

        write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))

        documents = get_documents(payload)
        root_html = as_text(payload.get("html"))
        if not root_html and documents:
            root_html = as_text(documents[0].get("html"))
        if root_html:
            write_text(html_path, root_html)

        document_result = save_documents(self.server.output_dir, base_name, payload)
        resource_result = save_fetched_resources(self.server.output_dir, base_name, payload)

        response = {
            "ok": True,
            "saved_json": str(json_path),
            "saved_html": str(html_path) if root_html else None,
            "saved_documents_dir": str(document_result["documents_dir"]) if document_result["documents_dir"] else None,
            "saved_documents_count": document_result["documents_count"],
            "saved_inline_dir": str(document_result["inline_dir"]) if document_result["inline_dir"] else None,
            "saved_inline_count": document_result["inline_count"],
            "saved_resources_dir": str(resource_result["resources_dir"]) if resource_result["resources_dir"] else None,
            "saved_resources_count": resource_result["resources_count"],
        }
        self._send_headers(200)
        self.wfile.write(json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8"))


def parse_args():
    parser = argparse.ArgumentParser(description="Receive webview dumps over HTTP and save them to disk.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on. Default: 8765")
    parser.add_argument(
        "--output-dir",
        default="webview_dumps",
        help="Directory where received dumps will be stored. Default: webview_dumps",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), DumpHandler)
    server.output_dir = output_dir

    print(f"Listening on http://{args.host}:{args.port}")
    print(f"Saving dumps to {output_dir}")
    print("POST webview dumps to /upload")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
