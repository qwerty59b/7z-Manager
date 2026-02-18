import asyncio
import datetime as dt
import functools
import math
import mimetypes
import os
import pathlib
import shutil
import socket
import threading
import time
import socketserver
from typing import Dict
from urllib.parse import quote, unquote, urlparse

import re
from dotenv import load_dotenv

import aiohttp
import humanize
import pyrogram.errors
from humanize import naturalsize
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from inspect import iscoroutinefunction as _iscoro
from pyrogram.handlers import MessageHandler as _MessageHandler


class ListenerTimeout(Exception):
    pass


_listeners = {}  # chat_id -> (asyncio.Future, filter | None)


async def _listener_intercept(client, message):
    """High-priority handler that resolves pending listen() futures."""
    chat_id = message.chat.id
    if chat_id not in _listeners:
        return
    future, filter_fn = _listeners[chat_id]
    if future.done():
        _listeners.pop(chat_id, None)
        return
    if filter_fn is not None:
        if _iscoro(filter_fn.__call__):
            matches = await filter_fn(client, message)
        else:
            matches = filter_fn(client, message)
        if not matches:
            return
    _listeners.pop(chat_id, None)
    future.set_result(message)
    raise pyrogram.StopPropagation


async def _listen(self, chat_id, filters=None, timeout=60):
    future = asyncio.get_running_loop().create_future()
    _listeners[chat_id] = (future, filters)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        _listeners.pop(chat_id, None)
        raise ListenerTimeout(timeout)


async def _ask(self, chat_id, text, filters=None, timeout=60):
    sent = await self.send_message(chat_id, text)
    response = await _listen(self, chat_id, filters=filters, timeout=timeout)
    response.request = sent
    return response


Client.listen = _listen
Client.ask = _ask

from functions import *

_MEGABYTE = 1048576

load_dotenv()

# Replace with your actual API credentials
API_ID: int = int(os.environ.get("API_ID"))
API_HASH: str = os.environ.get("API_HASH")
BOT_TOKEN: str = os.environ.get("BOT_TOKEN")
ADMIN_ID: int = int(os.environ.get("ADMIN_ID"))
MESSAGE_CHANNEL_ID: int = int(os.environ.get("MESSAGE_CHANNEL_ID"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost")

# Define the directory to serve files from
SERVE_DIRECTORY = pathlib.Path("public").absolute()
SERVE_DIRECTORY.mkdir(parents=True, exist_ok=True)

bot = Client("my_bot", api_hash=API_HASH, api_id=API_ID, bot_token=BOT_TOKEN)
bot.add_handler(_MessageHandler(_listener_intercept), group=-1)

users_list = {}  # user_id: {message_id: {'mime_type': ..., 'filename': ...}}
empty_list = "📝 Still no files to compress."
users_in_channel: Dict[int, dt.datetime] = dict()

def is_empty(user_id: str):
    return user_id not in users_list or not users_list[user_id]

@bot.on_message(filters=~(filters.private & filters.incoming))
async def on_chat_or_channel_message(client: Client, message: Message):
    pass  # Currently does nothing. You can implement logic if needed.

@bot.on_message()
async def on_private_message(client: Client, message: Message):
    channel = os.environ.get("CHANNEL", None)
    if not channel:
        return message.continue_propagation()
    if in_channel_cached := users_in_channel.get(message.from_user.id):
        if dt.datetime.now() - in_channel_cached < dt.timedelta(days=1):
            return message.continue_propagation()
    try:
        if await client.get_chat_member(channel, message.from_user.id):
            users_in_channel[message.from_user.id] = dt.datetime.now()
            return message.continue_propagation()
    except pyrogram.errors.UsernameNotOccupied:
        print("Channel does not exist, bot will continue to operate normally")
        return message.continue_propagation()
    except pyrogram.errors.ChatAdminRequired:
        print("Bot is not admin of the channel, bot will continue to operate normally")
        return message.continue_propagation()
    except pyrogram.errors.UserNotParticipant:
        await message.reply(
            "In order to use the bot, you must join its update channel.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Join!", url=f"t.me/{channel}")]]
            ),
        )

@bot.on_message(filters.video | filters.document | filters.audio)
async def filter_files(client, message):
    user_id = message.from_user.id
    media = getattr(message, message.media.value)
    mime_type = media.mime_type
    filename = (
        media.file_name
        or f"{media.file_unique_id}{mimetypes.guess_extension(mime_type) or ''}"
    )

    if user_id in users_list:
        users_list[user_id][message.id] = {"mime_type": mime_type, "filename": filename}
    else:
        users_list[user_id] = {
            message.id: {"mime_type": mime_type, "filename": filename}
        }

@bot.on_message(filters.command("start"))
async def start_command(client, message):
    text_to_send = """
Forward all the files you want to the bot and when you are ready to compress them send /compress
Specify the maximum size in MB of the zip or not if you don't want limits. Ex: __/compress 10__
To see the list of files to compress send /list and to clear the list to compress send /clear
Use /rename to rename a file in your list.
"""
    await message.reply_text(text_to_send)

@bot.on_message(filters.command("list"))
async def get_list(client, message):
    user_id = message.from_user.id
    if is_empty(user_id):
        text_to_send = empty_list
    else:
        text_to_send = "📝 List of files to compress by type:\n"
        for idx, (message_id, file_info) in enumerate(
            users_list[user_id].items(), start=1
        ):
            filename = file_info["filename"]
            mime_type = file_info["mime_type"]
            new_line = f"**{idx}. {filename}** : **{mime_type}**\n"

            if len(text_to_send + new_line) > 4096:
                await message.reply_text(text_to_send)
                text_to_send = new_line
            else:
                text_to_send += new_line

    await message.reply_text(text_to_send)

@bot.on_message(filters.command("clear"))
async def clear_list(client, message):
    users_list[message.from_user.id] = {}
    await message.reply_text("📝 List cleared.")

@bot.on_message(filters.command("cache_folder"))
async def show_cache_folder(client, message):
    dirpath = SERVE_DIRECTORY.joinpath(f"{message.from_user.id}")
    text = "📝 Temporary file list:\n"
    if dirpath.exists():
        for i, file in enumerate(sorted(dirpath.rglob("*.*"))):
            text += f"\n◾:{i}- **{file.name}** size: **{naturalsize(file.stat().st_size)}**"
        text += "\n\nUse **/clear_cache_folder** to remove them or **/compress** to retry compressing them."
    else:
        text += "Your temporary folder is empty."
    await message.reply_text(text)

@bot.on_message(filters.command("clear_cache_folder"))
async def clear_cache_folder(client, message):
    dirpath = SERVE_DIRECTORY.joinpath(f"{message.from_user.id}")
    if dirpath.exists():
        size = sum(file.stat().st_size for file in dirpath.rglob("*.*"))
        shutil.rmtree(str(dirpath.absolute()))
        await message.reply_text(
            f"Successfully deleted files. Freed up {naturalsize(size)}."
        )
    else:
        await message.reply_text(f"Your temporary folder is empty.")

@bot.on_message(filters.command("full_clear") & filters.user(ADMIN_ID))
async def full_clear(client, message):
    if len(os.listdir(SERVE_DIRECTORY)) == 0:
        await message.reply_text(
            "Directory is empty, nothing to do!"
        )
    else:
        size = sum(file.stat().st_size for file in SERVE_DIRECTORY.rglob("*.*"))

        for filename in os.listdir(SERVE_DIRECTORY):
            file_path = os.path.join(SERVE_DIRECTORY, filename)

            if os.path.isfile(file_path):
                os.remove(file_path)   # Delete files
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)  # Recursively delete subdirectories

        await message.reply_text(
            f"Successfully deleted every file. Freed up {naturalsize(size)}."
        )

@bot.on_message(filters.command("rename"))
async def rename_file(client, message):
    user_id = message.from_user.id
    if is_empty(user_id):
        await message.reply_text("Your file list is empty.")
        return

    # Display the list of files with indices
    file_list = users_list[user_id]
    file_options = ""
    for idx, (msg_id, file_info) in enumerate(file_list.items(), start=1):
        file_options += f"{idx}. {file_info['filename']}\n"

    prompt_message = await message.reply_text(
        f"Select the file number to rename:\n{file_options}"
    )
    try:
        response = await client.listen(chat_id=user_id, filters=filters.text, timeout=60)
    except ListenerTimeout:
        await prompt_message.edit_text("No response received. Operation cancelled.")
        return

    await prompt_message.delete()
    await response.delete()

    try:
        selected_idx = int(response.text.strip())
    except ValueError:
        await message.reply_text("Invalid input. Please enter a number.")
        return

    if selected_idx < 1 or selected_idx > len(file_list):
        await message.reply_text("Invalid selection.")
        return

    # Get the selected file
    selected_msg_id = list(file_list.keys())[selected_idx - 1]
    selected_file_info = file_list[selected_msg_id]
    old_filename = selected_file_info["filename"]

    # Ask for the new filename
    prompt_message = await message.reply_text(
        f"Enter the new name for **{old_filename}** (include the extension):"
    )
    try:
        response = await client.listen(chat_id=user_id, filters=filters.text, timeout=60)
    except ListenerTimeout:
        await prompt_message.edit_text("No response received. Operation cancelled.")
        return

    await prompt_message.delete()
    await response.delete()
    new_filename = response.text.strip()

    # Update the filename
    users_list[user_id][selected_msg_id]["filename"] = new_filename
    await message.reply_text(f"File renamed to **{new_filename}**.")

@bot.on_message(filters.command("download"))
async def download_from_url(client, message):
    user_id = message.from_user.id

    # --- Obtener URL(s) ---
    if message.reply_to_message and message.reply_to_message.text:
        raw_input = message.reply_to_message.text.strip()
    else:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply_text(
                "⚠️ Provide one or more URLs to download.\n"
                "Usage: `/download <URL1> <URL2> ...`"
            )
            return
        raw_input = args[1].strip()

    # Soporte para múltiples URLs (separadas por espacios o saltos de línea)
    urls = [u.strip() for u in re.split(r"[\s\n]+", raw_input) if u.strip()]

    # --- Validar URLs ---
    invalid_urls = [u for u in urls if not u.startswith(("http://", "https://"))]
    if invalid_urls:
        await message.reply_text(
            f"❌ Invalid URL(s):\n" + "\n".join(invalid_urls)
        )
        return

    user_dir = SERVE_DIRECTORY.joinpath(f"{user_id}").joinpath("files")
    user_dir.mkdir(parents=True, exist_ok=True)

    timeout = aiohttp.ClientTimeout(total=3600, connect=30)  # 1h total, 30s conexión

    for url in urls:
        progress_message = await message.reply_text(f"⏳ Starting download...\n`{url}`")
        start_time = time.time()

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        await progress_message.edit_text(
                            f"❌ Failed to download.\nURL: `{url}`\nHTTP Status: `{resp.status}`"
                        )
                        continue

                    total_size = int(resp.headers.get("content-length", 0))

                    # --- Detectar nombre de archivo ---
                    filename = _extract_filename(resp, url)
                    if not filename:
                        await progress_message.edit_text(
                            f"❌ Could not determine a filename for:\n`{url}`"
                        )
                        continue

                    # Evitar sobreescribir si ya existe
                    filepath = user_dir / filename
                    filepath = _unique_filepath(filepath)
                    filename = filepath.name

                    # --- Descargar con barra de progreso ---
                    downloaded = 0
                    chunk_size = _MEGABYTE  # 1 MB
                    last_edit = 0.0

                    with open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            f.write(chunk)
                            downloaded += len(chunk)

                            now = time.time()
                            if now - last_edit >= 20 or downloaded == total_size:
                                await progress_bar(
                                    downloaded,
                                    total_size or downloaded,
                                    "📥 Downloading:",
                                    start_time,
                                    progress_message,
                                    filename,
                                )
                                last_edit = now

                    # Leído dentro del bloque mientras la respuesta sigue abierta
                    mime_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()

            # --- Registrar en la lista del usuario con clave única ---
            file_key = int(time.time() * 1000)  # timestamp en ms como clave única
            if user_id not in users_list:
                users_list[user_id] = {}
            users_list[user_id][file_key] = {
                "mime_type": mime_type,
                "filename": filename,
            }

            await progress_message.edit_text(
                f"✅ Downloaded **{filename}** ({humanize.naturalsize(filepath.stat().st_size)}) "
                f"and added to your file list."
            )

        except asyncio.TimeoutError:
            await progress_message.edit_text(
                f"❌ Download timed out for:\n`{url}`"
            )
        except aiohttp.ClientError as e:
            await progress_message.edit_text(
                f"❌ Connection error for:\n`{url}`\n`{str(e)}`"
            )
        except Exception as e:
            await progress_message.edit_text(
                f"❌ Unexpected error: `{str(e)}`"
            )


def _extract_filename(resp: aiohttp.ClientResponse, url: str) -> str:
    """Extrae el nombre del archivo desde headers o URL, de forma robusta."""
    content_disposition = resp.headers.get("Content-Disposition", "")

    # Caso 1: filename*=UTF-8''nombre.zip  (RFC 5987)
    match = re.search(r"filename\*=UTF-8''([^\s;]+)", content_disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip().strip('"')

    # Caso 2: filename="nombre.zip" o filename=nombre.zip
    match = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', content_disposition, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"\'')

    # Caso 3: extraer del path de la URL
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = os.path.basename(path)
    if name and "." in name:
        return name

    # Caso 4: generar nombre desde Content-Type
    content_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".bin"
    return f"download_{int(time.time())}{ext}"


def _unique_filepath(filepath: pathlib.Path) -> pathlib.Path:
    """Si el archivo ya existe, agrega un sufijo numérico para no sobreescribir."""
    if not filepath.exists():
        return filepath
    stem = filepath.stem
    suffix = filepath.suffix
    parent = filepath.parent
    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1

@bot.on_message(filters.command("compress"))
async def compress(client, message):
    user_id = message.from_user.id
    if is_empty(user_id):
        await message.reply_text(empty_list)
        return

    user_dir = SERVE_DIRECTORY.joinpath(f"{user_id}").joinpath("files")
    user_dir.mkdir(parents=True, exist_ok=True)
    size = None
    args = message.text.strip().split()
    if len(args) > 1:
        try:
            size = int(args[1]) * _MEGABYTE  # Convert MB to bytes
        except ValueError:
            await message.reply_text(
                "Invalid size parameter. Please provide an integer value in MB."
            )
            return

    # Ask for the new filename
    try:
        file_name_message = await client.ask(
            chat_id=message.from_user.id,
            text="Send me the new filename for this task or send /cancel to stop.",
            filters=filters.text,
            timeout=60,
        )
    except ListenerTimeout:
        await message.reply_text("No response received. Operation cancelled.")
        return

    await file_name_message.request.delete()
    new_file_name = file_name_message.text
    if new_file_name.lower() == "/cancel":
        await message.delete()
        return

    # Ask for the password
    try:
        password_message = await client.ask(
            chat_id=message.from_user.id,
            text="Send me the password 🔒 for this task or send **NO** if you don't want.",
            filters=filters.text,
            timeout=60,
        )
    except ListenerTimeout:
        await message.reply_text("No response received. Operation cancelled.")
        return

    await password_message.request.delete()
    password = password_message.text

    if password.lower() == "no":
        password = None

    progress_download = await message.reply_text("Downloading 📥...")
    inicial = dt.datetime.now()

    for message_id in list(users_list[user_id].keys()):
        message_obj: Message = await client.get_messages(user_id, message_id)
        filename = users_list[user_id][message_id]["filename"]
        await download_file(message_obj, user_dir, progress_download, filename)
        users_list[user_id].pop(message_id)
    await progress_download.delete()
    await message.reply_text(
        f"Downloads finished in 📥 {humanize.naturaldelta(dt.datetime.now() - inicial)}."
    )
    await message.reply_text("Compression started 🗜")
    # NOTE: zip_files is a blocking function that can block the entire application, so it should run under to_thread()
    parts_path = await asyncio.to_thread(
        zip_files, user_dir, size,
        new_file_name, password
    )
    await message.reply_text("Compression finished 🗜")
    progress_upload = await message.reply_text("Uploading 📤...")
    inicial = dt.datetime.now()
    for file in sorted(parts_path.iterdir()):
        await upload_file(user_id, file, progress_upload)
    shutil.rmtree(str(parts_path.absolute()))
    await progress_upload.delete()
    await message.reply_text(
        f"Uploaded in 📤 {humanize.naturaldelta(dt.datetime.now() - inicial)}."
    )

async def download_file(
    message: Message, dirpath: pathlib.Path, progress_message: Message, filename: str
):
    filepath = dirpath.joinpath(filename)
    try:
        start_time = time.time()
        await message.download(
            file_name=str(filepath),
            progress=progress_bar,
            progress_args=("📥 Downloading:", start_time, progress_message, filename),
        )
    except Exception as e:
        print(e)

async def upload_file(user_id: str, file: pathlib.Path, progress_message: Message):
    try:
        start_time = time.time()
        await bot.send_document(
            user_id,
            str(file),
            progress=progress_bar,
            progress_args=("📤 Uploading:", start_time, progress_message, file.name),
        )
    except Exception as exc:
        print(exc)

async def progress_bar(current, total, status_msg, start, msg, filename):
    present = time.time()
    if round((present - start) % 20) == 0 or current == total:
        speed = current / (present - start) if present - start > 0 else 0
        percentage = current * 100 / total if total > 0 else 0
        time_to_complete = round(((total - current) / speed)) if speed > 0 else 0
        time_to_complete = humanize.naturaldelta(time_to_complete)
        progressbar = "[{0}{1}]".format(
            "".join(["🟢" for _ in range(math.floor(percentage / 10))]),
            "".join(["⚫" for _ in range(10 - math.floor(percentage / 10))]),
        )

        current_message = (
            f"""**{status_msg} {filename}** {round(percentage, 2)}%""" "\n"
            f"{progressbar}" "\n"
            f"**⚡ Speed**: {humanize.naturalsize(speed)}/s" "\n"
            f"**📚 Done**: {humanize.naturalsize(current)}" "\n"
            f"**💾 Size**: {humanize.naturalsize(total)}" "\n"
            f"**⏰ Time Left**: {time_to_complete}"
        )

        try:
            await msg.edit_text(current_message)
        except pyrogram.errors.MessageNotModified:
            pass

def split_file(file_path: pathlib.Path, max_size: int):
    parts_dir = file_path.parent / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_num = 1
    with open(file_path, "rb") as f:
        chunk = f.read(max_size)
        while chunk:
            part_path = parts_dir / f"{file_path.stem}.part{part_num}{file_path.suffix}"
            with open(part_path, "wb") as part_file:
                part_file.write(chunk)
            part_num += 1
            chunk = f.read(max_size)
    return parts_dir

async def start():
    print("Bot is running...")
    await bot.send_message(MESSAGE_CHANNEL_ID, "Bot has started.")

@bot.on_message(filters.command("link"))
async def generate_link(client, message):
    if not message.reply_to_message:
        user_id = message.from_user.id
        user_dir = SERVE_DIRECTORY.joinpath(f"{user_id}").joinpath("files")
        user_dir.mkdir(parents=True, exist_ok=True)
        relative_path = user_dir.relative_to(SERVE_DIRECTORY)
        dir_url = f"{PUBLIC_URL}/info/{relative_path.as_posix()}"
        await message.reply_text(dir_url)
        return

    replied_message = message.reply_to_message

    # Check if the message contains downloadable media
    media_types = ["document", "video", "audio", "photo"]
    media = None
    for media_type in media_types:
        media = getattr(replied_message, media_type, None)
        if media is not None:
            break

    if media is None:
        await message.reply_text(
            "The replied message doesn't contain any downloadable media."
        )
        return

    user_id = message.from_user.id
    user_dir = SERVE_DIRECTORY.joinpath(f"{user_id}").joinpath("files")
    user_dir.mkdir(parents=True, exist_ok=True)

    # Determine the filename
    if isinstance(media, pyrogram.types.Photo):
        filename = f"{media.file_unique_id}.jpg"
    else:
        filename = (
            media.file_name
            or f"{media.file_unique_id}{mimetypes.guess_extension(media.mime_type) or ''}"
        )

    filepath = user_dir / filename

    # Check if the file already exists
    if not filepath.exists():
        progress_message = await message.reply_text("Downloading the file...")
        try:
            await replied_message.download(
                file_name=str(filepath),
                progress=progress_bar,
                progress_args=(
                    "📥 Downloading:",
                    time.time(),
                    progress_message,
                    filename,
                ),
            )
            await progress_message.delete()
        except Exception as e:
            await progress_message.edit_text(f"Failed to download the file: {str(e)}")
            return

    # Generate the link
    relative_path = filepath.relative_to(SERVE_DIRECTORY)
    file_url = f"{PUBLIC_URL}/info/{quote(relative_path.as_posix())}"
    await message.reply_text(f"Here is your link:\n{file_url}")

if __name__ == "__main__":
    # W.I.P.
    # from sys import argv as sys_argv

    bot.start()
    asyncio.get_event_loop().run_until_complete(start())
    idle()
