import asyncio
import ssl
from typing import AsyncGenerator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import redis.asyncio as redis
from opal_common.logger import logger
from pydantic import BaseModel
from redis.asyncio.sentinel import Sentinel


class RedisDB:
    """Utility class to persist objects in Redis with Sentinel and SSL support.
    
    Supports both standard Redis URLs and Sentinel configurations:
    - Standard: redis://localhost:6379
    - Sentinel: redis+sentinel://sentinel1:26379,sentinel2:26379/mymaster?password=...
    """

    def __init__(
        self,
        redis_url: str,
        sentinel_kwargs: Optional[dict] = None,
        redis_kwargs: Optional[dict] = None,
    ):
        """Initialize Redis connection with optional Sentinel support.
        
        Args:
            redis_url: Redis connection string. Use 'redis+sentinel://' scheme for Sentinel.
            sentinel_kwargs: Additional kwargs for Sentinel connection
            redis_kwargs: Additional kwargs for Redis client connection
        """
        self._url = redis_url
        self._sentinel_kwargs = sentinel_kwargs or {}
        self._redis_kwargs = redis_kwargs or {}
        self._redis: Optional[redis.Redis] = None
        self._sentinel: Optional[Sentinel] = None
        self._is_sentinel = False
        self._service_name: Optional[str] = None
        self._reconnecting = False
        
        # Parse URL to determine connection type
        parsed = urlparse(redis_url)
        self._is_sentinel = parsed.scheme in ["redis+sentinel", "rediss+sentinel"]
        
        if self._is_sentinel:
            self._init_sentinel(parsed)
        else:
            self._init_standard(redis_url)

    def _init_standard(self, redis_url: str):
        """Initialize standard Redis connection."""
        logger.info("Connecting to Redis: {url}", url=self._mask_password(redis_url))
        self._redis = redis.Redis.from_url(redis_url, **self._redis_kwargs)

    def _init_sentinel(self, parsed: urlparse):
        """Initialize Redis Sentinel connection.
        
        URL format: redis+sentinel://sentinel1:26379,sentinel2:26379/mymaster?password=secret&ssl=true
        """
        # Extract sentinel hosts from netloc
        sentinel_hosts = self._parse_sentinel_hosts(parsed.netloc)
        
        # Extract service name from path (strip leading /)
        self._service_name = parsed.path.lstrip("/") if parsed.path else "mymaster"
        
        # Parse query parameters
        query_params = parse_qs(parsed.query)
        password = query_params.get("password", [None])[0]
        sentinel_password = query_params.get("sentinel_password", [None])[0]
        use_ssl = query_params.get("ssl", ["false"])[0].lower() == "true"
        ssl_cert_reqs = query_params.get("ssl_cert_reqs", ["required"])[0]
        ssl_ca_certs = query_params.get("ssl_ca_certs", [None])[0]
        
        # Build SSL context if needed
        ssl_context = None
        if use_ssl:
            ssl_context = self._create_ssl_context(ssl_cert_reqs, ssl_ca_certs)
        
        # Merge sentinel kwargs
        sentinel_config = {
            "password": sentinel_password,
            "ssl": use_ssl,
            "ssl_cert_reqs": ssl_cert_reqs if use_ssl and not ssl_context else None,
            "ssl_ca_certs": ssl_ca_certs if use_ssl and not ssl_context else None,
            **self._sentinel_kwargs,
        }
        
        # Remove None values
        sentinel_config = {k: v for k, v in sentinel_config.items() if v is not None}
        
        logger.info(
            "Connecting to Redis Sentinel: hosts={hosts}, service={service}, ssl={ssl}",
            hosts=sentinel_hosts,
            service=self._service_name,
            ssl=use_ssl,
        )
        
        # Create Sentinel instance
        self._sentinel = Sentinel(
            sentinel_hosts,
            sentinel_kwargs=sentinel_config,
            **self._redis_kwargs,
        )
        
        # Get master connection
        master_kwargs = {"password": password} if password else {}
        if ssl_context:
            master_kwargs["ssl"] = ssl_context
        
        self._redis = self._sentinel.master_for(
            self._service_name,
            **master_kwargs,
        )

    def _parse_sentinel_hosts(self, netloc: str) -> List[Tuple[str, int]]:
        """Parse comma-separated sentinel hosts.
        
        Format: sentinel1:26379,sentinel2:26379,sentinel3:26379
        """
        hosts = []
        for host_port in netloc.split(","):
            if ":" in host_port:
                host, port = host_port.rsplit(":", 1)
                hosts.append((host, int(port)))
            else:
                hosts.append((host_port, 26379))  # Default Sentinel port
        return hosts

    def _create_ssl_context(
        self, ssl_cert_reqs: str = "required", ssl_ca_certs: Optional[str] = None
    ) -> ssl.SSLContext:
        """Create SSL context for secure connections."""
        context = ssl.create_default_context()
        
        # Set certificate verification
        if ssl_cert_reqs == "none":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        elif ssl_cert_reqs == "optional":
            context.verify_mode = ssl.CERT_OPTIONAL
        else:  # required
            context.verify_mode = ssl.CERT_REQUIRED
        
        # Load CA certificates if provided
        if ssl_ca_certs:
            context.load_verify_locations(cafile=ssl_ca_certs)
        
        return context

    def _mask_password(self, url: str) -> str:
        """Mask password in URL for logging."""
        parsed = urlparse(url)
        if parsed.password:
            netloc = parsed.netloc.replace(f":{parsed.password}@", ":****@")
            return parsed._replace(netloc=netloc).geturl()
        
        # Mask query password
        if "password=" in url:
            return url.split("password=")[0] + "password=****"
        
        return url

    async def _reconnect(self):
        """Attempt to reconnect to Redis on connection failure with backoff.
        
        This method is called automatically when Redis operations fail.
        For Sentinel configurations, it will discover the new master.
        For standard Redis, it recreates the connection.
        
        Uses exponential backoff: 2s, 4s, 6s, 8s, 10s (max) intervals.
        """
        if self._reconnecting:
            # Already reconnecting, wait a bit
            await asyncio.sleep(0.1)
            return
            
        self._reconnecting = True
        max_backoff = 10  # Maximum 10 seconds
        backoff_interval = 2  # Start with 2 seconds
        attempt = 0
        
        try:
            logger.warning("Redis connection lost, attempting to reconnect...")
            
            while True:
                try:
                    if self._is_sentinel and self._sentinel:
                        # Sentinel will automatically discover new master
                        logger.info(
                            "Discovering new master via Sentinel: {service} (attempt {attempt})",
                            service=self._service_name,
                            attempt=attempt + 1,
                        )
                        # Get fresh master connection from Sentinel
                        parsed = urlparse(self._url)
                        query_params = parse_qs(parsed.query)
                        password = query_params.get("password", [None])[0]
                        use_ssl = query_params.get("ssl", ["false"])[0].lower() == "true"
                        ssl_cert_reqs = query_params.get("ssl_cert_reqs", ["required"])[0]
                        ssl_ca_certs = query_params.get("ssl_ca_certs", [None])[0]
                        
                        master_kwargs = {"password": password} if password else {}
                        if use_ssl:
                            ssl_context = self._create_ssl_context(ssl_cert_reqs, ssl_ca_certs)
                            master_kwargs["ssl"] = ssl_context
                        
                        # Close old connection
                        try:
                            await self._redis.close()
                        except Exception:
                            pass
                        
                        # Get new master
                        self._redis = self._sentinel.master_for(
                            self._service_name,
                            **master_kwargs,
                        )
                        await self._redis.ping()
                        logger.info("Successfully reconnected to Redis via Sentinel")
                        break
                    else:
                        # For standard Redis, recreate connection
                        logger.info("Recreating Redis connection (attempt {attempt})", attempt=attempt + 1)
                        try:
                            await self._redis.close()
                        except Exception:
                            pass
                            
                        self._init_standard(self._url)
                        await self._redis.ping()
                        logger.info("Successfully reconnected to Redis")
                        break
                        
                except Exception as e:
                    attempt += 1
                    # Calculate backoff with cap at max_backoff
                    current_backoff = min(backoff_interval * attempt, max_backoff)
                    logger.warning(
                        "Reconnection attempt {attempt} failed: {error}. Retrying in {backoff}s...",
                        attempt=attempt,
                        error=str(e),
                        backoff=current_backoff,
                    )
                    await asyncio.sleep(current_backoff)
                
        except Exception as e:
            logger.error(
                "Failed to reconnect to Redis after multiple attempts: {error}",
                error=str(e),
            )
            raise
        finally:
            self._reconnecting = False

    @property
    def redis_connection(self) -> redis.Redis:
        return self._redis

    async def set(self, key: str, value: BaseModel):
        try:
            await self._redis.set(key, self._serialize(value))
        except (redis.ConnectionError, redis.TimeoutError, OSError):
            await self._reconnect()
            await self._redis.set(key, self._serialize(value))

    async def set_if_not_exists(self, key: str, value: BaseModel) -> bool:
        """:param key:
        :param value:
        :return: True if created, False if key already exists
        """
        try:
            return await self._redis.set(key, self._serialize(value), nx=True)
        except (redis.ConnectionError, redis.TimeoutError, OSError):
            await self._reconnect()
            return await self._redis.set(key, self._serialize(value), nx=True)

    async def get(self, key: str) -> bytes:
        try:
            return await self._redis.get(key)
        except (redis.ConnectionError, redis.TimeoutError, OSError):
            await self._reconnect()
            return await self._redis.get(key)

    async def scan(self, pattern: str) -> AsyncGenerator[bytes, None]:
        try:
            cur = 0  # Redis returns integer cursor
            first_iteration = True
            while first_iteration or cur != 0:
                first_iteration = False
                cur, keys = await self._redis.scan(cur, match=pattern)

                for key in keys:
                    value = await self._redis.get(key)
                    yield value
        except (redis.ConnectionError, redis.TimeoutError, OSError):
            await self._reconnect()
            # Retry scan from beginning after reconnect
            cur = 0
            first_iteration = True
            while first_iteration or cur != 0:
                first_iteration = False
                cur, keys = await self._redis.scan(cur, match=pattern)

                for key in keys:
                    value = await self._redis.get(key)
                    yield value

    async def delete(self, key: str):
        try:
            await self._redis.delete(key)
        except (redis.ConnectionError, redis.TimeoutError, OSError):
            await self._reconnect()
            await self._redis.delete(key)

    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
        logger.info("Redis connection closed")

    def _serialize(self, value: BaseModel) -> str:
        return value.json()
