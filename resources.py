from typing import Optional, Tuple
from astrbot.api import logger
from urllib.parse import urlparse
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from datetime import datetime
import aiofiles
import aiofiles.os
import aiohttp
import errno
import shutil
import asyncio
import json
import os
import random


ONE_DAY_IN_SECONDS = 86400


class ResourceManager:
    """
    资源管理器，负责用户头像和背景图片的获取、缓存和管理
    """

    def __init__(self, plugin_config, plugin_name: Optional[str] = None) -> None:
        # 请求超时：整体 15 秒（含 DNS、握手、下载），连接阶段单独限制 5 秒
        self._http_timeout = aiohttp.ClientTimeout(total=15, connect=5)
        self._connection_limit = aiohttp.TCPConnector(limit=10)  # 限制并发连接数为10
        # trust_env=True 让 aiohttp 自动读取 HTTP_PROXY/HTTPS_PROXY 环境变量，走系统代理
        self._session = aiohttp.ClientSession(
            timeout=self._http_timeout,
            connector=self._connection_limit,
            trust_env=True,
        )
        self.plugin_config = plugin_config
        self.name = plugin_name or "astrbot_plugin_jrysprpr"

        self.avatar_cache_expiration = self.plugin_config.get(
            "avatar_cache_expiration", ONE_DAY_IN_SECONDS
        )  # 默认一天过期

        # 初始化jrys数据

        self.is_data_loaded = False

        self._storage_initialized = False
        self._plugin_data_dir: Optional[Path] = None
        self._background_cache_dir: Optional[Path] = None
        self._background_tmp_dir: Optional[Path] = None

        self.data_dir = os.path.dirname(os.path.abspath(__file__))
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.font_dir = os.path.join(self.data_dir, "font")

        # Lolicon API 配置：tag 过滤与 R18 模式
        self.api_tags = self.plugin_config.get("api_tags", [])
        self.api_r18 = self.plugin_config.get("api_r18", 0)

        self._http_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }

    async def get_background_image(self) -> Optional[Tuple[str, bool]]:
        """
        通过 Lolicon API 在线获取背景图片
        返回 (本地文件路径, 是否需要使用后清理) 二元组；失败返回 None
        """
        try:
            self._ensure_storage_dirs()
            return await self._get_background_from_api()

        except Exception as e:
            logger.error(f"获取背景图片时出错: {e}")
            return None

    # Pixiv 图片代理列表（含官方），按优先级排序
    # 官方 i.pximg.net 必须配合 Referer 才能访问，代理通常不需要
    # 环境能访问外网，优先走官方；官方失败再回落到代理
    PIXIV_PROXIES = [
        "https://i.pximg.net",
        "https://i.pixiv.nl",
        "https://i.phixiv.net",
    ]

    def _build_pixiv_mirror_urls(self, original_url: str) -> list[str]:
        """从原始 Pixiv URL 提取路径，构造多个镜像候选 URL"""
        parsed = urlparse(original_url)
        # 提取路径，如 /img-original/img/2024/01/01/00/00/00/12345678_p0.jpg
        path = parsed.path
        if not path or path == "/":
            return [original_url]
        # 去掉可能重复的前缀，保留标准路径
        # 某些代理返回的路径可能以 /img-original/ 或 /img-master/ 开头
        return [f"{base}{path}" for base in self.PIXIV_PROXIES]

    async def _get_background_from_api(self) -> Optional[Tuple[str, bool]]:
        """通过 Lolicon API 在线获取背景图"""
        image_url = await self._fetch_lolicon_api()
        if not image_url:
            logger.warning("API 未返回可用的图片 URL")
            return None

        # 已存在持久缓存：直接复用，不再下载，也不参与清理
        cache_path = self._background_cache_path_for_url(image_url)
        if cache_path.exists():
            return str(cache_path), False

        # 是否在使用后删除按需下载的临时文件（默认开启，避免占用磁盘）
        cleanup_downloads = bool(
            self.plugin_config.get("cleanup_background_downloads", True)
        )

        # cleanup_downloads=True 时下载到临时目录用完即删；False 则写入持久化缓存目录复用
        image_path = cache_path
        should_cleanup = False
        if cleanup_downloads:
            image_path = self._background_tmp_path_for_url(image_url)
            should_cleanup = True

        # 构造候选 URL 列表：如果是 Pixiv 图源则启用多代理轮询，否则只试原始 URL
        candidates = [image_url]
        if "pixiv" in image_url or "pximg" in image_url:
            candidates = self._build_pixiv_mirror_urls(image_url)

        last_error = None
        for idx, url in enumerate(candidates):
            # 官方域名必须带 Referer，否则 403；代理加了也无害
            extra_headers = {"Referer": "https://www.pixiv.net/"}

            # retries=0：单层代理只试一次，失败直接换下一个代理，避免日志嵌套
            ok = await self._download_to_path(
                url, image_path, label="背景图(API)", retries=0, extra_headers=extra_headers
            )
            if ok:
                logger.info(f"下载图片成功: {url}")
                return str(image_path), should_cleanup

            last_error = url
            logger.warning(f"背景图代理({idx + 1}/{len(candidates)}) 失败，尝试下一个: {url}")

        logger.warning(f"API 背景图下载失败，已耗尽所有代理。最后尝试: {last_error}")
        return None

    async def _fetch_lolicon_api(self) -> Optional[str]:
        """调用 Lolicon API 获取随机图片 URL"""
        try:
            url = "https://api.lolicon.app/setu/v2"
            payload = {
                "r18": self.api_r18,
                "num": 1,
                # 添加随机 uid 绕过 API 短期缓存
                "uid": random.randint(10000000, 99999999),
            }
            if self.api_tags:
                payload["tag"] = self.api_tags

            async with self._session.post(
                url, headers=self._http_headers, json=payload
            ) as response:
                if response.status != 200:
                    logger.error(f"Lolicon API 请求失败: HTTP {response.status}")
                    return None

                data = await response.json()
                if data.get("error"):
                    logger.error(f"Lolicon API 错误: {data['error']}")
                    return None

                results = data.get("data", [])
                if not results:
                    logger.warning("Lolicon API 未返回图片")
                    return None

                urls = results[0].get("urls", {})
                # 优先原图，fallback 到 regular
                return urls.get("original") or urls.get("regular")
        except Exception as e:
            logger.error(f"调用 Lolicon API 失败: {e}")
            return None

    async def fetch_setu_image(
        self, r18: int = 0, keyword: Optional[str] = None
    ) -> Optional[str]:
        """
        通过 Lolicon API 获取指定条件的涩图并下载到本地。
        成功返回本地文件路径（临时文件，调用方负责清理）；失败返回 None。
        """
        try:
            self._ensure_storage_dirs()
            url = "https://api.lolicon.app/setu/v2"
            
            # 构建请求 payload
            # 注意：uid 参数是指定 Pixiv 用户 ID，不是随机缓存绕过！
            base_payload = {
                "r18": max(0, min(2, r18)),
                "num": 1,
            }
            if keyword:
                base_payload["keyword"] = keyword
            
            # 策略1: 排除 AI 生成图片
            payload = {**base_payload, "excludeAI": True}
            logger.info(f"[Setu] 请求 Lolicon API (策略1: excludeAI) | r18={r18} keyword={keyword}")
            
            async with self._session.post(
                url, headers=self._http_headers, json=payload
            ) as response:
                if response.status != 200:
                    logger.error(f"[Setu] API 请求失败: HTTP {response.status}")
                    return None

                data = await response.json()
                # 记录完整响应用于调试
                logger.info(f"[Setu] API 响应: error={data.get('error')} data_length={len(data.get('data', []))}")
                
                if data.get("error"):
                    logger.error(f"[Setu] API 业务错误: {data['error']}")
                    return None

                results = data.get("data", [])
                
                # 策略2: 如果策略1无结果，尝试不排除 AI 图片
                if not results:
                    logger.warning(f"[Setu] 策略1无结果，尝试策略2(不排除AI)")
                    payload2 = {**base_payload, "excludeAI": False}
                    async with self._session.post(
                        url, headers=self._http_headers, json=payload2
                    ) as response2:
                        if response2.status == 200:
                            data2 = await response2.json()
                            logger.info(f"[Setu] API 响应(策略2): error={data2.get('error')} data_length={len(data2.get('data', []))}")
                            if not data2.get("error"):
                                results = data2.get("data", [])
                
                if not results:
                    logger.warning(f"[Setu] API 未返回图片 | 已尝试两种策略")
                    return None

                item = results[0]
                image_url = item.get("urls", {}).get("original")
                if not image_url:
                    logger.warning("[Setu] API 返回的 URL 为空")
                    return None

                logger.info(f"[Setu] API 返回图片 URL: {image_url}")

                # 下载到临时目录
                image_path = self._background_tmp_path_for_url(image_url)

                # 构造候选 URL 列表
                candidates = [image_url]
                if "pixiv" in image_url or "pximg" in image_url:
                    candidates = self._build_pixiv_mirror_urls(image_url)

                extra_headers = {"Referer": "https://www.pixiv.net/"}
                for idx, candidate_url in enumerate(candidates):
                    logger.info(
                        f"[Setu] 尝试下载 ({idx + 1}/{len(candidates)}): {candidate_url}"
                    )
                    ok = await self._download_to_path(
                        candidate_url,
                        image_path,
                        label="涩图",
                        retries=0,
                        extra_headers=extra_headers,
                    )
                    if ok:
                        logger.info(f"[Setu] 下载成功: {candidate_url}")
                        return str(image_path)
                    logger.warning(
                        f"[Setu] 代理({idx + 1}/{len(candidates)}) 失败，尝试下一个"
                    )

                logger.error("[Setu] 所有代理均下载失败")
                return None
        except Exception:
            logger.exception("[Setu] 获取涩图时发生异常")
            return None

    async def get_avatar_img(self, user_id: str) -> Optional[str]:
        """
        获取用户头像
          1. 获取用户头像2. 获取用户头像的 URL3. 下载头像4. 返回头像的路径
        Args:
            user_id (str): 用户 ID

        Returns:
            str: 头像的路径
        """
        try:
            self._ensure_storage_dirs()
            avatar_path = os.path.join(self.avatar_dir, f"{user_id}.jpg")
            # 检查头像是否存在
            if await aiofiles.os.path.exists(avatar_path):

                def _file_stat(path):
                    try:
                        st = os.stat(path)
                        return st.st_mtime
                    except FileNotFoundError:
                        return None

                file_mtime = await asyncio.to_thread(_file_stat, avatar_path)
                file_age = datetime.now().timestamp() - file_mtime
                if (
                    file_age < self.avatar_cache_expiration
                ):  # 默认如果头像文件小于一天，则不下载
                    return avatar_path

            url = f"http://q.qlogo.cn/g?b=qq&nk={user_id}&s=640"

            ok = await self._download_to_path(url, Path(avatar_path), label="头像")
            if ok:
                return avatar_path
            return None

        except Exception as e:
            logger.error(f"获取用户头像失败: {e}")
            return None

    async def initialize(self):
        """插件加载/重载后执行（适合做缓存预热等异步任务）。"""
        self._ensure_storage_dirs()

    def _migrate_legacy_cache_dir(
        self, legacy_dir: Path, target_dir: Path, label: str
    ) -> None:
        """将旧版本缓存目录迁移到标准插件数据目录。"""
        try:
            if not legacy_dir.exists() or not legacy_dir.is_dir():
                return

            legacy_resolved = legacy_dir.resolve()
            target_resolved = target_dir.resolve()
            if legacy_resolved == target_resolved:
                return

            target_dir.mkdir(parents=True, exist_ok=True)

            moved = 0
            skipped = 0
            replaced = 0
            failed = 0

            for item in legacy_dir.iterdir():
                if not item.is_file():
                    continue

                dest = target_dir / item.name
                try:
                    if dest.exists():
                        try:
                            src_stat = item.stat()
                            dest_stat = dest.stat()
                            if src_stat.st_mtime <= dest_stat.st_mtime:
                                item.unlink(missing_ok=True)
                                skipped += 1
                                continue
                        except Exception:
                            item.unlink(missing_ok=True)
                            skipped += 1
                            continue

                        replaced += 1

                    try:
                        os.replace(item, dest)
                    except OSError as e:
                        if e.errno == errno.EXDEV:
                            shutil.copy2(item, dest)
                            item.unlink(missing_ok=True)
                        else:
                            raise

                    moved += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"迁移{label}缓存失败: {item} -> {dest} | {e}")

            try:
                if not any(legacy_dir.iterdir()):
                    legacy_dir.rmdir()
            except Exception:
                pass

            if moved or replaced or skipped or failed:
                logger.info(
                    f"{label}缓存迁移完成: "
                    f"from={legacy_dir} to={target_dir} "
                    f"moved={moved} replaced={replaced} skipped={skipped} failed={failed}"
                )
        except Exception as e:
            logger.warning(f"{label}缓存迁移异常: {e}")

    def _ensure_storage_dirs(self) -> None:
        """初始化插件大文件缓存目录（优先 data/plugin_data/{plugin_name}）。"""
        if self._storage_initialized:
            return

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            plugin_name = (
                self.name or getattr(self, "name", None) or "astrbot_plugin_jrysprpr"
            )
            data_root = get_astrbot_data_path()
            data_root_path = (
                data_root if isinstance(data_root, Path) else Path(str(data_root))
            )
            plugin_data_dir = data_root_path / "plugin_data" / plugin_name
            plugin_data_dir.mkdir(parents=True, exist_ok=True)

            self._plugin_data_dir = plugin_data_dir

            cache_dir = plugin_data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_cache_dir = cache_dir / "background_images"
            self._background_cache_dir.mkdir(parents=True, exist_ok=True)
            self._background_tmp_dir = cache_dir / "background_images_tmp"
            self._background_tmp_dir.mkdir(parents=True, exist_ok=True)

            # 缓存目录分类：avatars / background_images / background_images_tmp
            target_avatar_dir = cache_dir / "avatars"
            self.avatar_dir = str(target_avatar_dir)
            os.makedirs(self.avatar_dir, exist_ok=True)

            # 迁移旧版本缓存目录（插件目录 / 旧 plugin_data 结构 / 旧 fallback 结构）
            legacy_avatar_dirs = [
                Path(self.data_dir) / "avatars",
                plugin_data_dir / "avatars",
            ]
            for legacy_dir in legacy_avatar_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, target_avatar_dir, label="头像"
                )

            legacy_background_dirs = [
                Path(self.data_dir) / "background_images",
                plugin_data_dir / "background_images",
            ]
            for legacy_dir in legacy_background_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_cache_dir, label="背景图"
                )

            legacy_background_tmp_dirs = [
                Path(self.data_dir) / "background_images_tmp",
                plugin_data_dir / "background_images_tmp",
            ]
            for legacy_dir in legacy_background_tmp_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_tmp_dir, label="背景图临时"
                )

            self._storage_initialized = True
            logger.info(f"插件数据目录初始化完成: {plugin_data_dir}")
        except Exception as e:
            # 兼容：若无法获取 AstrBot 数据目录，则回退到插件目录
            logger.warning(f"初始化插件数据目录失败，将回退到插件目录缓存: {e}")
            self._plugin_data_dir = Path(self.data_dir)

            cache_dir = self._plugin_data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_cache_dir = cache_dir / "background_images"
            self._background_cache_dir.mkdir(parents=True, exist_ok=True)
            self._background_tmp_dir = cache_dir / "background_images_tmp"
            self._background_tmp_dir.mkdir(parents=True, exist_ok=True)

            target_avatar_dir = cache_dir / "avatars"
            self.avatar_dir = str(target_avatar_dir)
            os.makedirs(self.avatar_dir, exist_ok=True)

            legacy_avatar_dirs = [
                Path(self.data_dir) / "avatars",
            ]
            for legacy_dir in legacy_avatar_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, target_avatar_dir, label="头像"
                )

            legacy_background_dirs = [
                Path(self.data_dir) / "background_images",
            ]
            for legacy_dir in legacy_background_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_cache_dir, label="背景图"
                )

            legacy_background_tmp_dirs = [
                Path(self.data_dir) / "background_images_tmp",
            ]
            for legacy_dir in legacy_background_tmp_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_tmp_dir, label="背景图临时"
                )

            self._storage_initialized = True

    def _background_cache_path_for_url(self, url: str) -> Path:
        self._ensure_storage_dirs()
        assert self._background_cache_dir is not None

        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 10:
            ext = ".img"
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self._background_cache_dir / f"{digest}{ext}"

    def _background_tmp_path_for_url(self, url: str) -> Path:
        self._ensure_storage_dirs()
        assert self._background_tmp_dir is not None

        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 10:
            ext = ".img"
        return self._background_tmp_dir / f"{uuid4().hex}{ext}"

    async def _download_to_path(
        self, url: str, dest: Path, label: str = "图片", retries: int = 1, extra_headers: Optional[dict] = None
    ) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        retries = max(0, int(retries))

        # 合并自定义请求头（如 Pixiv Referer）
        headers = dict(self._http_headers)
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(retries + 1):
            status: Optional[int] = None
            reason = ""
            tmp_path = dest.parent / f"{dest.name}.{uuid4().hex}.tmp"

            try:
                async with self._session.get(
                    url, headers=headers
                ) as response:
                    status = response.status
                    reason = (response.reason or "").strip()

                    if status < 200 or status >= 300:
                        # 5xx 可能是临时问题，允许重试；其它状态码直接失败
                        if 500 <= status <= 599 and attempt < retries:
                            logger.warning(
                                f"{label}下载失败({attempt + 1}/{retries + 1}): HTTP {status} {reason} | {url}"
                            )
                            continue

                        logger.error(f"{label}下载失败: HTTP {status} {reason} | {url}")
                        return False

                    # 流式写入，避免一次性读入内存
                    async with aiofiles.open(tmp_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            await f.write(chunk)

                await asyncio.to_thread(os.replace, tmp_path, dest)
                return True
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}Timeout | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(f"{label}下载失败: {http_info}Timeout | {url}")
            except aiohttp.ClientPayloadError as e:
                msg = str(e).strip()
                # 该类错误通常带有较长的内部异常信息，保持简短即可
                if ":" in msg:
                    msg = msg.split(":", 1)[0].strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            except aiohttp.ClientError as e:
                msg = str(e).strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            except Exception as e:
                msg = str(e).strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

        return False

    async def _load_jrys_data(self) -> dict:
        """
        初始化 jrys.json 文件
        1. 检查当前目录下是否存在 jrys.json 文件
        2. 如果不存在，则创建一个空的 jrys.json 文件
        3. 如果存在，则读取文件内容
        4. 如果文件内容不是有效的 JSON 格式，则打印错误信息
        """

        if self.is_data_loaded:
            return self.jrys_data

        jrys_path = os.path.join(self.data_dir, "jrys.json")

        # 检查 jrys.json 文件是否存在,如果不存在，则创建一个空的 jrys.json 文件
        if not os.path.exists(jrys_path):
            async with aiofiles.open(jrys_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps({}))
                logger.info(f"创建空的运势数据文件: {jrys_path}")

        # 读取 JSON 文件
        try:
            async with aiofiles.open(jrys_path, "r", encoding="utf-8") as f:
                content = await f.read()
                # json.loads是CPU密集型，用 to_thread 包装
                self.jrys_data = await asyncio.to_thread(json.loads, content)
                self.is_data_loaded = True  # 标记数据已加载
                logger.info(f"读取运势数据文件: {jrys_path}")

            return self.jrys_data

        except FileNotFoundError:
            logger.error(f"文件 {jrys_path} 没找到")
            return {}
        except json.JSONDecodeError:
            logger.error(f"文件 {jrys_path} 不是有效的 JSON 格式")
            return {}

    async def _save_jrys_data(self):
        """保存 jrys 数据到 jrys.json"""
        jrys_path = os.path.join(self.data_dir, "jrys.json")
        try:
            async with aiofiles.open(jrys_path, "w", encoding="utf-8") as f:
                content = await asyncio.to_thread(
                    json.dumps, self.jrys_data, ensure_ascii=False, indent=4
                )
                await f.write(content)
        except Exception as e:
            logger.error(f"保存运势数据失败: {e}")
