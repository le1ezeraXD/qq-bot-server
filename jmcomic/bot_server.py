import asyncio
import base64
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import aiohttp
import botpy
from botpy.http import Route
from botpy.message import GroupMessage
from dotenv import load_dotenv
from jmcomic import Feature, download_album

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
ENV_FILE = Path(__file__).with_name(".env")

BASE64_FILE_LIMIT = 6 * 1024 * 1024
MAX_FILE_SIZE = 100 * 1024 * 1024
MD5_10M_SIZE = 10_002_432
UPLOAD_RETRIES = 3

load_dotenv(ENV_FILE)
APPID = os.getenv("QQ_BOT_APPID")
SECRET = os.getenv("QQ_BOT_SECRET")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("jmcomic-bot")


def extract_album_id(content: str) -> str | None:
    """从“JM123456”或“123456其他文字”中提取车牌号。"""
    match = re.search(r"(?:JM\s*)?(\d{3,})", content, flags=re.IGNORECASE)
    return match.group(1) if match else None


def download_jm(album_id: str) -> Path:
    """下载本子、导出 PDF，并返回 PDF 路径。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUTPUT_DIR / f"{album_id}.pdf"

    if pdf_path.is_file() and pdf_path.stat().st_size > 0:
        logger.info("复用已有 PDF：%s", pdf_path.name)
        return pdf_path

    download_album(
        album_id,
        extra=Feature.export_pdf(
            pdf_dir=str(OUTPUT_DIR),
            filename_rule="Aid",
            delete_original_file=True,
        ),
    )

    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise FileNotFoundError(f"下载结束，但没有找到有效的 {pdf_path.name}")

    return pdf_path


def compute_file_hashes(path: Path) -> dict[str, str]:
    """计算 QQ 分片上传要求的 MD5、SHA1 和文件前 10002432 字节 MD5。"""
    md5_hash = hashlib.md5()
    sha1_hash = hashlib.sha1()
    md5_10m_hash = hashlib.md5()
    first_bytes_remaining = MD5_10M_SIZE

    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            md5_hash.update(chunk)
            sha1_hash.update(chunk)
            if first_bytes_remaining > 0:
                first_chunk = chunk[:first_bytes_remaining]
                md5_10m_hash.update(first_chunk)
                first_bytes_remaining -= len(first_chunk)

    md5 = md5_hash.hexdigest()
    return {
        "md5": md5,
        "sha1": sha1_hash.hexdigest(),
        "md5_10m": md5 if path.stat().st_size <= MD5_10M_SIZE else md5_10m_hash.hexdigest(),
    }


def read_file_chunk(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as file:
        file.seek(offset)
        return file.read(length)


async def put_part(session: aiohttp.ClientSession, url: str, data: bytes, index: int) -> None:
    """将单个分片 PUT 到 QQ 返回的预签名地址。"""
    last_error: Exception | None = None
    for attempt in range(UPLOAD_RETRIES):
        try:
            async with session.put(
                url,
                data=data,
                headers={"Content-Length": str(len(data))},
            ) as response:
                if 200 <= response.status < 300:
                    return
                body = await response.text()
                raise RuntimeError(f"分片 {index} PUT 失败：HTTP {response.status} {body[:200]}")
        except Exception as exc:
            last_error = exc
            if attempt < UPLOAD_RETRIES - 1:
                await asyncio.sleep(2**attempt)

    raise RuntimeError(f"分片 {index} 上传重试耗尽") from last_error


async def post_with_retry(
    message: GroupMessage,
    route: Route,
    payload: dict[str, Any],
    action: str,
) -> Any:
    """重试 QQ 大文件协议中的确认和完成请求。"""
    last_error: Exception | None = None
    for attempt in range(UPLOAD_RETRIES):
        try:
            return await message._api._http.request(route, json=payload)
        except Exception as exc:
            last_error = exc
            if attempt < UPLOAD_RETRIES - 1:
                await asyncio.sleep(2**attempt)

    raise RuntimeError(f"{action}重试耗尽") from last_error


async def chunked_upload_group_pdf(message: GroupMessage, pdf_path: Path) -> dict[str, Any]:
    """按照 QQ 官方大文件协议分片上传 PDF。"""
    file_size = pdf_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        raise ValueError("PDF 超过 QQ 附件 100 MB 上限")

    hashes = await asyncio.to_thread(compute_file_hashes, pdf_path)
    prepare_route = Route(
        "POST",
        "/v2/groups/{group_openid}/upload_prepare",
        group_openid=message.group_openid,
    )
    prepared = await message._api._http.request(
        prepare_route,
        json={
            "file_type": 4,
            "file_name": pdf_path.name,
            "file_size": file_size,
            **hashes,
        },
    )

    upload_id = prepared["upload_id"]
    block_size = int(prepared["block_size"])
    parts = prepared["parts"]
    finish_route = Route(
        "POST",
        "/v2/groups/{group_openid}/upload_part_finish",
        group_openid=message.group_openid,
    )

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for part in parts:
            index = int(part["index"])
            offset = (index - 1) * block_size
            length = min(block_size, file_size - offset)
            data = await asyncio.to_thread(read_file_chunk, pdf_path, offset, length)
            part_md5 = hashlib.md5(data).hexdigest()

            logger.info("上传 PDF 分片 %s/%s（%s 字节）", index, len(parts), length)
            await put_part(session, part["presigned_url"], data, index)
            await post_with_retry(
                message,
                finish_route,
                {
                    "upload_id": upload_id,
                    "part_index": index,
                    "block_size": len(data),
                    "md5": part_md5,
                },
                f"确认分片 {index}",
            )

    complete_route = Route(
        "POST",
        "/v2/groups/{group_openid}/files",
        group_openid=message.group_openid,
    )
    return await post_with_retry(
        message,
        complete_route,
        {"upload_id": upload_id},
        "完成文件上传",
    )


async def upload_group_pdf(message: GroupMessage, pdf_path: Path) -> dict[str, Any]:
    file_size = pdf_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        raise ValueError("PDF 超过 QQ 附件 100 MB 上限")
    if file_size > BASE64_FILE_LIMIT:
        return await chunked_upload_group_pdf(message, pdf_path)

    file_data = await asyncio.to_thread(
        lambda: base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    )
    route = Route(
        "POST",
        "/v2/groups/{group_openid}/files",
        group_openid=message.group_openid,
    )
    return await message._api._http.request(
        route,
        json={
            "file_type": 4,
            "file_data": file_data,
            "file_name": pdf_path.name,
            "srv_send_msg": False,
        },
    )


class MyClient(botpy.Client):
    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._album_locks: dict[str, asyncio.Lock] = {}

    async def get_album_pdf(self, album_id: str) -> Path:
        """避免同一 JM 号被多人同时下载和合并。"""
        lock = self._album_locks.setdefault(album_id, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(download_jm, album_id)

    async def on_group_at_message_create(self, message: GroupMessage):
        content = message.content.strip()

        if "你好" in content:
            await message.reply(
                content=(
                    "你好，我是 JM 猕猴桃。将 JM 车牌号发给我，我可以下载对应内容。\n"
                    "例如：JM123456 或 123456"
                )
            )
            return

        album_id = extract_album_id(content)
        if not album_id:
            await message.reply(content="没有识别到 JM 号，请发送例如：JM123456")
            return

        try:
            await message.reply(content=f"收到 JM{album_id}，正在处理……")
            pdf_path = await self.get_album_pdf(album_id)
            media = await upload_group_pdf(message, pdf_path)

            await message.reply(
                msg_type=7,
                content="",
                media={"file_info": media["file_info"]},
                msg_seq=2,
            )

            member_openid = message.author.member_openid
            mention = f"<@{member_openid}>" if member_openid else ""
            await message.reply(
                content=f"{mention} 你的文件已下载好".strip(),
                msg_seq=3,
            )
        except Exception as exc:
            logger.exception("处理 JM%s 失败", album_id)
            try:
                await message.reply(
                    content=f"JM{album_id} 处理失败，请稍后重试。",
                    msg_seq=4,
                )
            except Exception:
                logger.exception("向群聊发送失败通知时发生异常")


def main() -> None:
    missing = [name for name, value in {"QQ_BOT_APPID": APPID, "QQ_BOT_SECRET": SECRET}.items() if not value]
    if missing:
        raise RuntimeError(f"缺少配置：{', '.join(missing)}。请复制 jmcomic/.env.example 为 jmcomic/.env 后填写。")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    intents = botpy.Intents(public_messages=True)
    client = MyClient(intents=intents)
    client.run(appid=APPID, secret=SECRET)


if __name__ == "__main__":
    main()
