
import os
import re
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename

app = Flask(__name__)

APP_NAME = "PlayerAudio Hi-Res FLAC"
APP_VERSION = "1.0.0"
USER_AGENT = "PlayerAudio/1.0"

BASE_URL = "https://archive.org"
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = (10, 60)


def get_metadata(identifier):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
        raise ValueError("Invalid identifier")

    url = f"{BASE_URL}/metadata/{identifier}"
    response = requests.get(
        url, headers=HEADERS, timeout=TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def get_flac_file(identifier, filename):
    metadata = get_metadata(identifier)

    for item in metadata.get("files", []):
        if (
            item.get("name") == filename
            and filename.lower().endswith(".flac")
            and item.get("private") not in (True, "true")
        ):
            return item

    return None


def verify_flac(identifier, filename):
    url = (
        f"{BASE_URL}/download/{identifier}/"
        f"{quote(filename, safe='/')}"
    )

    headers = dict(HEADERS)
    headers["Range"] = "bytes=0-3"

    response = requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=TIMEOUT
    )

    try:
        if response.status_code not in (200, 206):
            return False

        first_bytes = next(response.iter_content(4), b"")
        return first_bytes == b"fLaC"

    finally:
        response.close()


@app.route("/")
def home():
    return jsonify({
        "name": APP_NAME,
        "version": APP_VERSION,
        "status": "online"
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "id": "com.playeraudio.hiresflac",
        "version": APP_VERSION,
        "name": APP_NAME,
        "description": "FLAC audio search, streaming and downloads",
        "resources": ["catalog", "stream"],
        "types": ["music"],
        "catalogs": [{
            "type": "music",
            "id": "playeraudio-search",
            "name": "PlayerAudio Search"
        }],
        "behaviorHints": {
            "configurable": False
        }
    })


def search_archive(query):
    url = f"{BASE_URL}/advancedsearch.php"

    params = [
        ("q", f"title:({query}) AND mediatype:audio"),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "creator"),
        ("rows", 20),
        ("page", 1),
        ("output", "json")
    ]

    response = requests.get(
        url,
        params=params,
        headers=HEADERS,
        timeout=TIMEOUT
    )
    response.raise_for_status()

    return response.json().get("response", {}).get("docs", [])


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()

    if not query:
        return jsonify({"error": "Query is required"}), 400

    try:
        results = search_archive(query)
        tracks = []

        for result in results:
            identifier = result.get("identifier")

            if not identifier:
                continue

            try:
                metadata = get_metadata(identifier)
                flac = None

                for item in metadata.get("files", []):
                    name = item.get("name", "")

                    if (
                        name.lower().endswith(".flac")
                        and item.get("private") not in (True, "true")
                    ):
                        flac = item
                        break

                if not flac:
                    continue

                filename = flac["name"]
                encoded_name = quote(filename, safe="/")

                tracks.append({
                    "id": identifier,
                    "title": result.get("title", "Unknown"),
                    "artist": result.get("creator", "Unknown"),
                    "format": "FLAC",
                    "size": flac.get("size"),
                    "artwork": (
                        f"{BASE_URL}/services/img/{identifier}"
                    ),
                    "stream": (
                        f"/stream/{identifier}/{encoded_name}"
                    ),
                    "download": (
                        f"/download/{identifier}/{encoded_name}"
                    )
                })

            except (requests.RequestException, ValueError):
                continue

        return jsonify({
            "query": query,
            "count": len(tracks),
            "tracks": tracks
        })

    except requests.RequestException:
        return jsonify({
            "error": "Unable to search Internet Archive"
        }), 502


@app.route("/stream/<identifier>/<path:filename>")
def stream(identifier, filename):
    try:
        flac = get_flac_file(identifier, filename)

        if not flac:
            return jsonify({"error": "FLAC file not found"}), 404

        if not verify_flac(identifier, filename):
            return jsonify({
                "error": "FLAC signature verification failed"
            }), 415

        url = (
            f"{BASE_URL}/download/{identifier}/"
            f"{quote(filename, safe='/')}"
        )

        return jsonify({
            "format": "FLAC",
            "url": url,
            "status": "verified-header"
        })

    except ValueError:
        return jsonify({"error": "Invalid identifier"}), 400

    except requests.RequestException:
        return jsonify({"error": "Source unavailable"}), 502


@app.route("/download/<identifier>/<path:filename>")
def download(identifier, filename):
    try:
        flac = get_flac_file(identifier, filename)

        if not flac:
            return jsonify({"error": "FLAC file not found"}), 404

        if not verify_flac(identifier, filename):
            return jsonify({
                "error": "FLAC signature verification failed"
            }), 415

        url = (
            f"{BASE_URL}/download/{identifier}/"
            f"{quote(filename, safe='/')}"
        )

        headers = dict(HEADERS)

        # Forward Range requests so supported downloads can resume.
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header

        upstream = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=TIMEOUT
        )

        if upstream.status_code not in (200, 206):
            status = upstream.status_code
            upstream.close()
            return jsonify({
                "error": "Download source returned an error",
                "source_status": status
            }), status

        safe_name = secure_filename(filename.split("/")[-1])
        if not safe_name:
            safe_name = "audio.flac"

        response_headers = {
            "Content-Type": "audio/flac",
            "Content-Disposition": (
                f'attachment; filename="{safe_name}"'
            )
        }

        for header in (
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "ETag",
            "Last-Modified"
        ):
            if header in upstream.headers:
                response_headers[header] = upstream.headers[header]

        def generate():
            try:
                for chunk in upstream.iter_content(
                    chunk_size=64 * 1024
                ):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=response_headers,
            direct_passthrough=True
        )

    except ValueError:
        return jsonify({"error": "Invalid identifier"}), 400

    except requests.RequestException:
        return jsonify({"error": "Download failed"}), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
)
  
