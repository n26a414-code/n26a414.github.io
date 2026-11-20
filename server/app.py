from __future__ import annotations

import ctypes
import os
import shutil
import threading
import time
import secrets
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "api_token.txt"
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
API_TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()
ALLOWED_ORIGINS = {
    x.strip() for x in os.environ.get("ALLOWED_ORIGINS", "").split(",") if x.strip()
}
DEFAULT_OUTPUT_DIR = Path(os.environ.get("MP3_OUTPUT_DIR", str(Path.home() / "Music" / "MP3")))
_lock = threading.Lock()
_job = {"running": False, "logs": [], "success": None, "output_file": None}


def add_log(message: str) -> None:
    with _lock:
        _job["logs"].append({"time": time.strftime("%H:%M:%S"), "message": str(message)})
        _job["logs"] = _job["logs"][-250:]


class YtDlpLogger:
    def debug(self, msg):
        if msg and not str(msg).startswith("[debug]"):
            add_log(msg)

    def warning(self, msg):
        add_log("警告: " + str(msg))

    def error(self, msg):
        add_log("エラー: " + str(msg))


def valid_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and host in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"
        }
    except Exception:
        return False


def windows_roots() -> list[Path]:
    roots = []
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                roots.append(Path(f"{chr(65 + i)}:/"))
    else:
        roots.append(Path("/"))
    return roots


def allowed_roots() -> list[Path]:
    roots = windows_roots()
    home = Path.home().resolve()
    if not any(home == r.resolve() for r in roots if r.exists()):
        roots.append(home)
    return roots


def resolve_browsable(path_text: str | None) -> Path:
    roots = allowed_roots()
    if not path_text:
        # Open the user's home folder initially; drive roots remain available in the root list.
        candidate = Path.home()
    else:
        candidate = Path(path_text).expanduser()
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("指定したフォルダが見つかりません。")
    if not candidate.is_dir():
        raise ValueError("指定先はフォルダではありません。")
    if not any(candidate == root.resolve() or root.resolve() in candidate.parents for root in roots if root.exists()):
        raise ValueError("この場所はフォルダ選択の対象外です。")
    return candidate


def convert_worker(url: str, output_dir: Path) -> None:
    try:
        add_log("動画情報を確認しています...")
        info_opts = {"quiet": True, "noplaylist": True, "logger": YtDlpLogger()}
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            add_log("タイトル: " + str(info.get("title", "unknown")))
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "windowsfilenames": True,
            "noplaylist": True,
            "logger": YtDlpLogger(),
            "quiet": True,
            "no_warnings": False,
            "progress_hooks": [progress_hook],
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            # yt-dlpの出力名を取得してから変換する（日本語ファイル名にも対応）
            info = ydl.extract_info(url, download=False)
            prepared_name = ydl.prepare_filename(info)
            mp3_path = Path(prepared_name).with_suffix(".mp3")
            ydl.download([url])

        # 出力先が指定フォルダ内にあることを確認してから公開する
        mp3_path = mp3_path.resolve(strict=True)
        output_root = output_dir.resolve(strict=True)
        if output_root not in mp3_path.parents or not mp3_path.is_file():
            raise RuntimeError("作成されたMP3ファイルを確認できませんでした。")

        add_log("MP3の作成が完了しました。保存先: " + str(output_dir))
        with _lock:
            _job["success"] = True
            _job["output_file"] = str(mp3_path)
    except Exception as exc:
        add_log("処理に失敗しました: " + str(exc))
        with _lock:
            _job["success"] = False
    finally:
        with _lock:
            _job["running"] = False


def progress_hook(data):
    if data.get("status") == "downloading":
        percent = str(data.get("_percent_str", "")).strip()
        speed = str(data.get("_speed_str", "")).strip()
        add_log(f"ダウンロード中: {percent} 速度: {speed}")
    elif data.get("status") == "finished":
        add_log("ダウンロード完了。MP3へ変換しています...")


@app.before_request
def protect_api():
    if not request.path.startswith("/api/") or request.method == "OPTIONS":
        return None
    origin = request.headers.get("Origin", "")
    if origin and ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        return jsonify({"error": "このWebサイトからの接続は許可されていません。"}), 403
    supplied = request.headers.get("X-API-Token", "")
    if not secrets.compare_digest(supplied, API_TOKEN):
        return jsonify({"error": "APIトークンが正しくありません。"}), 401
    return None


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin and (not ALLOWED_ORIGINS or origin in ALLOWED_ORIGINS):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/api/<path:_path>", methods=["OPTIONS"])
def options(_path):
    return ("", 204)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "yt_dlp": yt_dlp is not None, "ffmpeg": shutil.which("ffmpeg") is not None,
                    "default_output_dir": str(DEFAULT_OUTPUT_DIR), "default_output_exists": DEFAULT_OUTPUT_DIR.is_dir()})


@app.get("/api/browse")
def browse():
    try:
        current = resolve_browsable(request.args.get("path"))
        dirs = []
        try:
            for item in current.iterdir():
                try:
                    if item.is_dir():
                        resolved = item.resolve()
                        if any(resolved == root.resolve() or root.resolve() in resolved.parents for root in allowed_roots() if root.exists()):
                            dirs.append({"name": item.name, "path": str(resolved)})
                except (OSError, RuntimeError, PermissionError):
                    continue
        except PermissionError:
            return jsonify({"error": "このフォルダを開く権限がありません。"}), 403
        dirs.sort(key=lambda x: x["name"].casefold())
        roots = [{"name": str(r), "path": str(r)} for r in allowed_roots() if r.exists()]
        return jsonify({"current": str(current), "parent": str(current.parent) if current.parent != current else None,
                        "directories": dirs, "roots": roots})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/status")
def status():
    with _lock:
        return jsonify({"running": _job["running"], "logs": list(_job["logs"]), "success": _job["success"]})


@app.get("/api/download")
def download_latest():
    with _lock:
        output_file = _job.get("output_file")

    if not output_file:
        return jsonify({"error": "ダウンロードできるMP3がありません。変換を完了してください。"}), 404

    try:
        file_path = Path(output_file).resolve(strict=True)
        if not file_path.is_file() or file_path.suffix.lower() != ".mp3":
            return jsonify({"error": "MP3ファイルが見つかりません。"}), 404
        return send_file(
            file_path,
            as_attachment=True,
            download_name=file_path.name,
            mimetype="audio/mpeg",
            conditional=True
        )
    except (OSError, RuntimeError):
        return jsonify({"error": "MP3ファイルを読み込めませんでした。"}), 404


@app.post("/api/start")
def start():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    output_text = str(data.get("output_dir", "")).strip()
    confirmed = data.get("confirmed") is True
    if not url or not valid_youtube_url(url):
        return jsonify({"error": "有効なYouTube URLを入力してください。"}), 400
    if not confirmed:
        return jsonify({"error": "保存・変換する権利または許可があることを確認してください。"}), 400
    if yt_dlp is None:
        return jsonify({"error": "yt-dlpがありません。setup.batを実行してください。"}), 500
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "FFmpegが見つかりません。インストールしてPATHを設定してください。"}), 500
    try:
        if output_text:
            output_dir = resolve_browsable(output_text)
        else:
            DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            output_dir = resolve_browsable(str(DEFAULT_OUTPUT_DIR))
        # 書き込み可能かを実際に確認する
        probe = output_dir / f".mp3_write_test_{os.getpid()}"
        probe.touch(exist_ok=False)
        probe.unlink()
    except FileExistsError:
        return jsonify({"error": "保存先の確認用ファイル名が使用中です。もう一度お試しください。"}), 400
    except (ValueError, OSError, PermissionError) as exc:
        return jsonify({"error": f"保存先を使用できません: {exc}"}), 400
    with _lock:
        if _job["running"]:
            return jsonify({"error": "別の変換処理が実行中です。完了してから再度お試しください。"}), 409
        _job["running"] = True
        _job["logs"] = []
        _job["success"] = None
        _job["output_file"] = None
    threading.Thread(target=convert_worker, args=(url, output_dir), daemon=True).start()
    return jsonify({"started": True, "output_dir": str(output_dir)})


if __name__ == "__main__":
    print("APIトークン:", API_TOKEN)
    print("GitHub PagesのURLを ALLOWED_ORIGINS に設定してください。")
    print("MP3変換バックエンド起動: http://0.0.0.0:5000")
    print("初期保存先:", DEFAULT_OUTPUT_DIR)
    print("同じWi-Fi内の信頼できる端末からのみ利用してください。")
    app.run(host="0.0.0.0", port=5000, threaded=True)
