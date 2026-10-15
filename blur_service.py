"""
Geração das amostras (teasers) embaçadas.

- Fotos: baixa o arquivo do Telegram, aplica GaussianBlur com Pillow e devolve um BytesIO (JPEG).
- Vídeos: se o FFmpeg estiver instalado, extrai um frame do vídeo e embaça;
  caso contrário, gera uma capa genérica "VIDEO BLOQUEADO".

O processamento de imagem é CPU-bound, por isso roda em uma thread
(asyncio.to_thread) para não travar o loop do bot.
"""
import asyncio
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from PIL import Image, ImageDraw, ImageFilter, ImageFont

MAX_LADO = 1280          # redimensiona antes de embaçar (mais rápido e blur mais uniforme)
TEXTO_SELO = "CONTEUDO BLOQUEADO"   # sem acento: a fonte padrão do Pillow não tem todos os glifos


def _selo(img: Image.Image, texto: str) -> Image.Image:
    """Desenha uma faixa escura com o texto no centro da imagem."""
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    tamanho = max(18, w // 18)
    try:
        fonte = ImageFont.load_default(size=tamanho)   # Pillow >= 10.1
    except TypeError:
        fonte = ImageFont.load_default()
    x0, y0, x1, y1 = draw.textbbox((0, 0), texto, font=fonte)
    tw, th = x1 - x0, y1 - y0
    pad = tamanho // 2
    caixa = [(w - tw) / 2 - pad, (h - th) / 2 - pad, (w + tw) / 2 + pad, (h + th) / 2 + pad]
    draw.rounded_rectangle(caixa, radius=pad, fill=(0, 0, 0, 150))
    draw.text(((w - tw) / 2 - x0, (h - th) / 2 - y0), texto, font=fonte, fill=(255, 255, 255, 235))
    return img


def aplicar_blur(dados: bytes, radius: int = 20, selo: str | None = TEXTO_SELO) -> BytesIO:
    """Função síncrona e pura: bytes da imagem -> JPEG embaçado em memória."""
    img = Image.open(BytesIO(dados))
    img = img.convert("RGB")
    img.thumbnail((MAX_LADO, MAX_LADO))
    if radius:
        img = img.filter(ImageFilter.GaussianBlur(radius=radius))
    if selo:
        img = _selo(img, selo)
    saida = BytesIO()
    img.save(saida, format="JPEG", quality=85)
    saida.seek(0)
    return saida


def _capa_video_generica() -> bytes:
    img = Image.new("RGB", (960, 540), (30, 30, 40))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


async def _baixar(bot: Bot, file_id: str) -> bytes:
    """Baixa um arquivo do Telegram para a memória (limite da Bot API: 20 MB)."""
    arquivo = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(arquivo.file_path, destination=buf)
    return buf.getvalue()


async def blur_foto_telegram(bot: Bot, file_id: str, radius: int = 20) -> BytesIO:
    """Recebe o file_id de uma foto do Telegram e devolve a versão embaçada (BytesIO)."""
    dados = await _baixar(bot, file_id)
    return await asyncio.to_thread(aplicar_blur, dados, radius)


def _frame_ffmpeg(dados_video: bytes) -> bytes | None:
    """Extrai um frame (no segundo 1) usando FFmpeg. Retorna None se falhar."""
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        entrada, saida = Path(tmp, "in.mp4"), Path(tmp, "frame.jpg")
        entrada.write_bytes(dados_video)
        for ts in ("1", "0"):
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", ts, "-i", str(entrada),
                 "-frames:v", "1", str(saida)],
                capture_output=True, timeout=60,
            )
            if proc.returncode == 0 and saida.exists():
                return saida.read_bytes()
    return None


async def teaser_video_telegram(bot: Bot, file_id: str, radius: int = 20) -> BytesIO:
    """Gera uma imagem-teaser embaçada para um vídeo."""
    frame = None
    if shutil.which("ffmpeg"):
        dados = await _baixar(bot, file_id)
        frame = await asyncio.to_thread(_frame_ffmpeg, dados)
    if frame is None:
        frame, radius = _capa_video_generica(), 0
    return await asyncio.to_thread(aplicar_blur, frame, radius, "VIDEO BLOQUEADO")
