import os
import uuid
from urllib.parse import urlparse
from urllib.request import url2pathname, urlopen
from PIL import Image, ImageFilter
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image as CompImage, File as CompFile, Reply as CompReply
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 兼容不同版本的 Pillow 常量
try:
    FLIP_LEFT_RIGHT = Image.Transpose.FLIP_LEFT_RIGHT
    FLIP_TOP_BOTTOM = Image.Transpose.FLIP_TOP_BOTTOM
except AttributeError:
    FLIP_LEFT_RIGHT = Image.FLIP_LEFT_RIGHT
    FLIP_TOP_BOTTOM = Image.FLIP_TOP_BOTTOM


class Main(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 初始化数据存储路径
        self.data_dir = os.path.join(
            str(get_astrbot_data_path()),
            "plugin_data",
            "astrbot_plugin_image_toolkit",
        )
        self.temp_dir = os.path.join(self.data_dir, "temp")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        logger.info("astrbot_plugin_image_toolkit 初始化完成。")

    def _get_config(self, key: str, default=None):
        """安全获取配置项"""
        return self.config.get(key, default)

    def _is_image_file_url(self, url: str) -> bool:
        """判断 File 组件 URL 是否是常见图片文件"""
        if not url:
            return False
        lower_url = url.lower().split("?", 1)[0]
        return lower_url.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))

    def _normalize_local_path(self, img_path: str) -> str | None:
        """将本地路径或 file:// URI 归一化为可访问的本地路径"""
        if not img_path:
            return None

        normalized_path = img_path.strip()
        if normalized_path.startswith("file://"):
            parsed = urlparse(normalized_path)
            normalized_path = url2pathname(parsed.path)
            if parsed.netloc and not normalized_path.startswith("\\"):
                normalized_path = f"\\\\{parsed.netloc}{normalized_path}"
        elif urlparse(normalized_path).scheme in ("http", "https"):
            return None

        normalized_path = os.path.expanduser(normalized_path)
        normalized_path = os.path.normpath(normalized_path)

        if os.path.exists(normalized_path):
            return normalized_path

        # 兼容只传文件名的场景，尝试在常见临时目录中补全
        basename = os.path.basename(normalized_path)
        if basename == normalized_path and self._is_image_file_url(basename):
            candidate_dirs = [
                self.temp_dir,
                self.data_dir,
                os.path.join(str(get_astrbot_data_path()), "temp"),
            ]
            for candidate_dir in candidate_dirs:
                candidate_path = os.path.join(candidate_dir, basename)
                candidate_path = os.path.normpath(candidate_path)
                if os.path.exists(candidate_path):
                    return candidate_path

        # 兼容 LLM 工具传入的正斜杠 Windows 路径，如 C:/Users/xxx/a.jpg
        if os.name == "nt" and ":/" in normalized_path:
            alt_path = os.path.normpath(normalized_path.replace("/", "\\"))
            if os.path.exists(alt_path):
                return alt_path

        return None

    def _download_remote_image(self, url: str) -> str | None:
        """下载远程图片到临时目录并返回本地路径"""
        if not url or not url.startswith(("http://", "https://")):
            return None

        try:
            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path)[1].lower()
            if not ext:
                ext = ".png"
            temp_path = os.path.join(self.temp_dir, f"{uuid.uuid4().hex}{ext}")
            with urlopen(url, timeout=15) as response, open(temp_path, "wb") as f:
                f.write(response.read())
            return temp_path if os.path.exists(temp_path) else None
        except Exception as e:
            logger.error(f"下载远程图片失败: {e}")
            return None

    def _collect_image_sources(self, event: AstrMessageEvent) -> list[str]:
        """从当前消息和引用消息中收集图片来源"""
        sources = []
        seen = set()

        def append_source(source):
            if source and source not in seen:
                seen.add(source)
                sources.append(source)

        def append_component_candidates(comp):
            for attr in ("file", "url", "path"):
                value = getattr(comp, attr, None)
                if isinstance(value, str) and value.strip():
                    ext = os.path.splitext(value)[1].lower()
                    if attr == "url" or self._is_image_file_url(value) or ext in (
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".webp",
                        ".gif",
                        ".bmp",
                    ):
                        append_source(value.strip())

        def append_source_from_component(comp):
            if isinstance(comp, CompImage):
                append_component_candidates(comp)
            elif isinstance(comp, CompFile):
                append_component_candidates(comp)

        message_groups = []
        try:
            message_groups.append(event.get_messages() or [])
        except Exception as e:
            logger.warning(f"读取 event.get_messages() 失败: {e}")

        raw_message = getattr(getattr(event, "message_obj", None), "message", None)
        if raw_message:
            message_groups.append(raw_message)

        for message_group in message_groups:
            for comp in message_group:
                append_source_from_component(comp)
                if isinstance(comp, CompReply) and getattr(comp, "chain", None):
                    for quote_comp in comp.chain:
                        append_source_from_component(quote_comp)

        return sources

    def _extract_image_path(self, event: AstrMessageEvent) -> str | None:
        """从消息中提取第一张可处理图片的本地路径"""
        for img_source in self._collect_image_sources(event):
            logger.info(f"检测图片来源: {img_source}")
            local_path = self._normalize_local_path(img_source)
            if local_path:
                logger.info(f"命中本地图片路径: {local_path}")
                return local_path

            if img_source.startswith(("http://", "https://")):
                downloaded_path = self._download_remote_image(img_source)
                if downloaded_path:
                    logger.info(f"远程图片已下载到: {downloaded_path}")
                    return downloaded_path

        logger.warning("未从消息中提取到可用图片路径")
        return None

    def _convert_to_rgb(self, img: Image.Image) -> Image.Image:
        """安全地将图片转换为 RGB（带透明通道的替换为白色背景）"""
        if img.mode in ["RGBA", "LA", "P"]:
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode in ["RGBA", "LA"]:
                background.paste(img, mask=img.split()[-1])
            else:
                rgba_img = img.convert("RGBA")
                background.paste(rgba_img, mask=rgba_img.split()[-1])
            return background
        elif img.mode != "RGB":
            return img.convert("RGB")
        return img

    def _save_image(self, img: Image.Image, original_path: str) -> str:
        """保存处理后的图片并返回路径"""
        out_fmt_setting = self._get_config("default_output_format", "original")

        if out_fmt_setting == "original":
            ext = os.path.splitext(original_path)[1].lower()
            if not ext:
                ext = ".png"
        else:
            ext = f".{out_fmt_setting.lower()}"

        if ext in [".jpg", ".jpeg"]:
            img = self._convert_to_rgb(img)

        filename = f"{uuid.uuid4().hex}{ext}"
        save_path = os.path.join(self.data_dir, filename)

        save_kwargs = {}
        if ext in [".jpg", ".jpeg", ".webp"]:
            save_kwargs["quality"] = self._get_config("default_convert_quality", 75)

        img.save(save_path, **save_kwargs)
        return save_path

    def _cleanup_file(self, file_path: str):
        """清理临时文件"""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"清理临时图片失败: {e}")

    @filter.command("img_info")
    async def img_info(self, event: AstrMessageEvent):
        '''查看图片的详细信息（格式、尺寸、大小等）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                file_size = os.path.getsize(img_path)
                if file_size > 1024 * 1024:
                    size_str = f"{file_size / (1024 * 1024):.2f} MB"
                else:
                    size_str = f"{file_size / 1024:.2f} KB"

                info = (
                    f"📷 图片信息\n"
                    f"格式: {img.format or '未知'}\n"
                    f"模式: {img.mode}\n"
                    f"尺寸: {img.width} x {img.height} px\n"
                    f"大小: {size_str}"
                )
                yield event.plain_result(info)
        except Exception as e:
            logger.error(f"读取图片信息失败: {e}")
            yield event.plain_result(f"读取图片信息失败: {str(e)}")

    @filter.command("img_resize")
    async def img_resize(self, event: AstrMessageEvent, width: int, height: int):
        '''调整图片尺寸（需指定宽度和高度）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        if width <= 0 or height <= 0:
            yield event.plain_result("宽度和高度必须大于0。")
            return

        try:
            with Image.open(img_path) as img:
                resized_img = img.resize((width, height))
                save_path = self._save_image(resized_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"调整尺寸失败: {e}")
            yield event.plain_result(f"调整尺寸失败: {str(e)}")

    @filter.command("img_crop")
    async def img_crop(self, event: AstrMessageEvent, left: int, top: int, right: int, bottom: int):
        '''裁剪图片（需指定左上角和右下角坐标）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        if left >= right or top >= bottom:
            yield event.plain_result("裁剪坐标无效，左上角坐标必须小于右下角坐标。")
            return

        try:
            with Image.open(img_path) as img:
                cropped_img = img.crop((left, top, right, bottom))
                save_path = self._save_image(cropped_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"裁剪图片失败: {e}")
            yield event.plain_result(f"裁剪图片失败: {str(e)}")

    @filter.command("img_rotate")
    async def img_rotate(self, event: AstrMessageEvent, angle: int):
        '''旋转图片（需指定旋转角度）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                rotated_img = img.rotate(angle, expand=True)
                save_path = self._save_image(rotated_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"旋转图片失败: {e}")
            yield event.plain_result(f"旋转图片失败: {str(e)}")

    @filter.command("img_gray")
    async def img_gray(self, event: AstrMessageEvent):
        '''将图片转换为灰度图'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                gray_img = img.convert("L")
                save_path = self._save_image(gray_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"灰度化失败: {e}")
            yield event.plain_result(f"灰度化失败: {str(e)}")

    @filter.command("img_blur")
    async def img_blur(self, event: AstrMessageEvent, radius: int = 0):
        '''对图片应用模糊效果（可指定模糊半径）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        if radius <= 0:
            radius = self._get_config("default_blur_radius", 2)

        if radius <= 0:
            yield event.plain_result("模糊半径必须大于0。")
            return

        try:
            with Image.open(img_path) as img:
                blurred_img = img.filter(ImageFilter.GaussianBlur(radius=radius))
                save_path = self._save_image(blurred_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"模糊处理失败: {e}")
            yield event.plain_result(f"模糊处理失败: {str(e)}")

    @filter.command("img_convert")
    async def img_convert(self, event: AstrMessageEvent, target_format: str, quality: int = 0):
        '''转换图片格式或进行压缩（可指定目标格式和质量）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        target_format = target_format.lower()
        if target_format not in ["png", "jpeg", "jpg", "webp"]:
            yield event.plain_result("不支持的目标格式，目前仅支持 png, jpeg, webp。")
            return

        if quality <= 0:
            quality = self._get_config("default_convert_quality", 75)

        quality = max(1, min(100, quality))

        try:
            with Image.open(img_path) as img:
                if target_format in ["jpeg", "jpg"]:
                    img = self._convert_to_rgb(img)

                ext = f".{target_format}"
                filename = f"{uuid.uuid4().hex}{ext}"
                save_path = os.path.join(self.data_dir, filename)

                pil_format = "JPEG" if target_format in ["jpg", "jpeg"] else target_format.upper()

                save_kwargs = {}
                if pil_format in ["JPEG", "WEBP"]:
                    save_kwargs["quality"] = quality

                img.save(save_path, format=pil_format, **save_kwargs)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"格式转换失败: {e}")
            yield event.plain_result(f"格式转换失败: {str(e)}")

    @filter.command("img_mirror_lr")
    async def img_mirror_lr(self, event: AstrMessageEvent):
        '''以竖直中轴线为参考，将左半区镜像到右半区'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                mirrored_img = img.copy()
                width, height = img.size
                split_x = width // 2
                left_half = img.crop((0, 0, split_x, height))
                mirrored_right = left_half.transpose(FLIP_LEFT_RIGHT)
                paste_x = width - mirrored_right.width
                mirrored_img.paste(mirrored_right, (paste_x, 0))
                save_path = self._save_image(mirrored_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"左右轴对称处理失败: {e}")
            yield event.plain_result(f"左右轴对称处理失败: {str(e)}")

    @filter.command("img_mirror_ud")
    async def img_mirror_ud(self, event: AstrMessageEvent):
        '''以水平中轴线为参考，将上半区镜像到下半区'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                mirrored_img = img.copy()
                width, height = img.size
                split_y = height // 2
                top_half = img.crop((0, 0, width, split_y))
                mirrored_bottom = top_half.transpose(FLIP_TOP_BOTTOM)
                paste_y = height - mirrored_bottom.height
                mirrored_img.paste(mirrored_bottom, (0, paste_y))
                save_path = self._save_image(mirrored_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"上下轴对称处理失败: {e}")
            yield event.plain_result(f"上下轴对称处理失败: {str(e)}")

    @filter.command("img_mirror_rl")
    async def img_mirror_rl(self, event: AstrMessageEvent):
        '''以竖直中轴线为参考，将右半区镜像到左半区'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                mirrored_img = img.copy()
                width, height = img.size
                split_x = width // 2
                right_half = img.crop((split_x, 0, width, height))
                mirrored_left = right_half.transpose(FLIP_LEFT_RIGHT)
                mirrored_img.paste(mirrored_left, (0, 0))
                save_path = self._save_image(mirrored_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"右到左轴对称处理失败: {e}")
            yield event.plain_result(f"右到左轴对称处理失败: {str(e)}")

    @filter.command("img_mirror_du")
    async def img_mirror_du(self, event: AstrMessageEvent):
        '''以水平中轴线为参考，将下半区镜像到上半区'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                mirrored_img = img.copy()
                width, height = img.size
                split_y = height // 2
                bottom_half = img.crop((0, split_y, width, height))
                mirrored_top = bottom_half.transpose(FLIP_TOP_BOTTOM)
                mirrored_img.paste(mirrored_top, (0, 0))
                save_path = self._save_image(mirrored_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"下到上轴对称处理失败: {e}")
            yield event.plain_result(f"下到上轴对称处理失败: {str(e)}")

    @filter.command("img_flip_lr")
    async def img_flip_lr(self, event: AstrMessageEvent):
        '''整张图片水平镜像翻转（左右翻转）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                flipped_img = img.transpose(FLIP_LEFT_RIGHT)
                save_path = self._save_image(flipped_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"整图左右翻转失败: {e}")
            yield event.plain_result(f"整图左右翻转失败: {str(e)}")

    @filter.command("img_flip_ud")
    async def img_flip_ud(self, event: AstrMessageEvent):
        '''整张图片垂直镜像翻转（上下翻转）'''
        img_path = self._extract_image_path(event)
        if not img_path:
            yield event.plain_result("未检测到可用图片，请发送图片，或回复一张图片后再执行命令。")
            return

        try:
            with Image.open(img_path) as img:
                flipped_img = img.transpose(FLIP_TOP_BOTTOM)
                save_path = self._save_image(flipped_img, img_path)
                yield event.image_result(save_path)
        except Exception as e:
            logger.error(f"整图上下翻转失败: {e}")
            yield event.plain_result(f"整图上下翻转失败: {str(e)}")

    @filter.command("img_help")
    async def img_help(self, event: AstrMessageEvent):
        '''查看图片工具指令说明'''
        help_text = (
            "🖼️ 图片工具指令\n"
            "\n"
            "基础信息\n"
            "- img_info：查看图片格式、尺寸、大小\n"
            "\n"
            "基础处理\n"
            "- img_resize 宽 高：调整尺寸\n"
            "- img_crop left top right bottom：裁剪图片\n"
            "- img_rotate angle：旋转图片\n"
            "- img_gray：转灰度图\n"
            "- img_blur [radius]：模糊处理\n"
            "- img_convert 格式 [quality]：格式转换/压缩，支持 png/jpeg/jpg/webp\n"
            "\n"
            "半边轴对称\n"
            "- img_mirror_lr：左半区镜像到右半区\n"
            "- img_mirror_rl：右半区镜像到左半区\n"
            "- img_mirror_ud：上半区镜像到下半区\n"
            "- img_mirror_du：下半区镜像到上半区\n"
            "\n"
            "整图翻转\n"
            "- img_flip_lr：整张图左右翻转\n"
            "- img_flip_ud：整张图上下翻转\n"
            "\n"
            "使用方式\n"
            "- 直接发送图片并附带指令\n"
            "- 或回复一张图片后再执行指令"
        )
        yield event.plain_result(help_text)
