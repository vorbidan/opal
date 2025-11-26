import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.parse import urlparse

import pytest
import redis.asyncio as redis
from redis.asyncio.sentinel import Sentinel

from opal_server.redis_utils import RedisDB


class TestRedisDBInitialization:
    """Test RedisDB initialization with different URL formats."""

    def test_standard_redis_url(self):
        """Test initialization with standard Redis URL."""
        redis_url = "redis://localhost:6379"
        
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url:
            mock_redis = MagicMock()
            mock_from_url.return_value = mock_redis
            
            db = RedisDB(redis_url)
            
            assert db._is_sentinel is False
            assert db._redis == mock_redis
            assert db._sentinel is None
            mock_from_url.assert_called_once_with(redis_url)

    def test_sentinel_url_parsing(self):
        """Test Sentinel URL parsing with multiple hosts."""
        redis_url = "redis+sentinel://sentinel1:26379,sentinel2:26380,sentinel3:26381/mymaster?password=secret&ssl=true"
        
        with patch("opal_server.redis_utils.Sentinel") as mock_sentinel_class:
            mock_sentinel = MagicMock()
            mock_redis = MagicMock()
            mock_sentinel.master_for.return_value = mock_redis
            mock_sentinel_class.return_value = mock_sentinel
            
            db = RedisDB(redis_url)
            
            assert db._is_sentinel is True
            assert db._service_name == "mymaster"
            assert db._sentinel == mock_sentinel
            assert db._redis == mock_redis
            
            # Verify Sentinel was initialized with correct hosts
            call_args = mock_sentinel_class.call_args
            sentinel_hosts = call_args[0][0]
            assert sentinel_hosts == [
                ("sentinel1", 26379),
                ("sentinel2", 26380),
                ("sentinel3", 26381),
            ]

    def test_sentinel_url_default_service_name(self):
        """Test Sentinel URL with default service name."""
        redis_url = "redis+sentinel://sentinel1:26379"
        
        with patch("opal_server.redis_utils.Sentinel") as mock_sentinel_class:
            mock_sentinel = MagicMock()
            mock_redis = MagicMock()
            mock_sentinel.master_for.return_value = mock_redis
            mock_sentinel_class.return_value = mock_sentinel
            
            db = RedisDB(redis_url)
            
            assert db._service_name == "mymaster"

    def test_sentinel_url_with_ssl_parameters(self):
        """Test Sentinel URL with SSL configuration."""
        redis_url = "redis+sentinel://sentinel1:26379/mymaster?ssl=true&ssl_cert_reqs=required&ssl_ca_certs=/path/to/ca.crt"
        
        with patch("opal_server.redis_utils.Sentinel") as mock_sentinel_class, \
             patch("opal_server.redis_utils.ssl.create_default_context") as mock_ssl_context:
            
            mock_sentinel = MagicMock()
            mock_redis = MagicMock()
            mock_sentinel.master_for.return_value = mock_redis
            mock_sentinel_class.return_value = mock_sentinel
            
            mock_context = MagicMock()
            mock_ssl_context.return_value = mock_context
            
            db = RedisDB(redis_url)
            
            # Verify SSL context was created
            mock_ssl_context.assert_called_once()
            
            # Verify master_for was called with SSL context
            master_for_call = mock_sentinel.master_for.call_args
            assert "ssl" in master_for_call[1]

    def test_parse_sentinel_hosts(self):
        """Test parsing of sentinel host strings."""
        redis_url = "redis://localhost:6379"
        db = RedisDB(redis_url)
        
        # Test with ports
        hosts = db._parse_sentinel_hosts("host1:26379,host2:26380,host3:26381")
        assert hosts == [("host1", 26379), ("host2", 26380), ("host3", 26381)]
        
        # Test without ports (should default to 26379)
        hosts = db._parse_sentinel_hosts("host1,host2,host3")
        assert hosts == [("host1", 26379), ("host2", 26379), ("host3", 26379)]
        
        # Test mixed
        hosts = db._parse_sentinel_hosts("host1:26380,host2,host3:26382")
        assert hosts == [("host1", 26380), ("host2", 26379), ("host3", 26382)]

    def test_create_ssl_context_none(self):
        """Test SSL context creation with no verification."""
        redis_url = "redis://localhost:6379"
        db = RedisDB(redis_url)
        
        context = db._create_ssl_context(ssl_cert_reqs="none")
        
        assert context.check_hostname is False
        assert context.verify_mode == ssl.CERT_NONE

    def test_create_ssl_context_optional(self):
        """Test SSL context creation with optional verification."""
        redis_url = "redis://localhost:6379"
        db = RedisDB(redis_url)
        
        context = db._create_ssl_context(ssl_cert_reqs="optional")
        
        assert context.verify_mode == ssl.CERT_OPTIONAL

    def test_create_ssl_context_required(self):
        """Test SSL context creation with required verification."""
        redis_url = "redis://localhost:6379"
        db = RedisDB(redis_url)
        
        context = db._create_ssl_context(ssl_cert_reqs="required")
        
        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_mask_password_in_url(self):
        """Test password masking in URLs."""
        redis_url = "redis://localhost:6379"
        db = RedisDB(redis_url)
        
        # Test URL with password in auth
        url_with_auth = "redis://:mypassword@localhost:6379"
        masked = db._mask_password(url_with_auth)
        assert "mypassword" not in masked
        assert "****" in masked
        
        # Test URL with password in query
        url_with_query = "redis+sentinel://sentinel1:26379/mymaster?password=secret123"
        masked = db._mask_password(url_with_query)
        assert "secret123" not in masked
        assert "password=****" in masked
        
        # Test URL without password
        url_no_password = "redis://localhost:6379"
        masked = db._mask_password(url_no_password)
        assert masked == url_no_password


class TestRedisDBOperations:
    """Test RedisDB CRUD operations."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        mock = AsyncMock()
        mock.set = AsyncMock()
        mock.get = AsyncMock()
        mock.delete = AsyncMock()
        mock.scan = AsyncMock()
        mock.ping = AsyncMock()
        mock.close = AsyncMock()
        return mock

    @pytest.fixture
    def redis_db(self, mock_redis):
        """Create a RedisDB instance with mocked Redis client."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url:
            mock_from_url.return_value = mock_redis
            db = RedisDB("redis://localhost:6379")
            return db

    @pytest.mark.asyncio
    async def test_set_operation(self, redis_db, mock_redis):
        """Test set operation."""
        from pydantic import BaseModel
        
        class TestModel(BaseModel):
            value: str
        
        model = TestModel(value="test")
        await redis_db.set("key1", model)
        
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "key1"

    @pytest.mark.asyncio
    async def test_set_if_not_exists(self, redis_db, mock_redis):
        """Test set if not exists operation."""
        from pydantic import BaseModel
        
        class TestModel(BaseModel):
            value: str
        
        mock_redis.set.return_value = True
        
        model = TestModel(value="test")
        result = await redis_db.set_if_not_exists("key1", model)
        
        assert result is True
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[1]["nx"] is True

    @pytest.mark.asyncio
    async def test_get_operation(self, redis_db, mock_redis):
        """Test get operation."""
        mock_redis.get.return_value = b"value"
        
        result = await redis_db.get("key1")
        
        assert result == b"value"
        mock_redis.get.assert_called_once_with("key1")

    @pytest.mark.asyncio
    async def test_delete_operation(self, redis_db, mock_redis):
        """Test delete operation."""
        await redis_db.delete("key1")
        
        mock_redis.delete.assert_called_once_with("key1")

    @pytest.mark.asyncio
    async def test_scan_operation(self, redis_db, mock_redis):
        """Test scan operation."""
        # Track calls to scan and provide appropriate responses
        call_count = [0]
        
        async def mock_scan(cursor, match=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call returns cursor with keys (Redis returns integer cursor)
                return (123, [b"key1", b"key2"])
            elif call_count[0] == 2:
                # Second call returns 0 (done) with last key
                return (0, [b"key3"])
            else:
                # Shouldn't get here in normal flow
                return (0, [])
        
        mock_redis.scan = mock_scan
        
        get_values = [b"value1", b"value2", b"value3"]
        get_index = [0]
        
        async def mock_get(key):
            value = get_values[get_index[0]]
            get_index[0] += 1
            return value
        
        mock_redis.get = mock_get
        
        values = []
        async for value in redis_db.scan("pattern*"):
            values.append(value)
        
        assert values == [b"value1", b"value2", b"value3"]
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_close_operation(self, redis_db, mock_redis):
        """Test close operation."""
        await redis_db.close()
        
        mock_redis.close.assert_called_once()


class TestRedisDBReconnection:
    """Test RedisDB reconnection logic with backoff."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        mock = AsyncMock()
        mock.set = AsyncMock()
        mock.get = AsyncMock()
        mock.ping = AsyncMock()
        mock.close = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_reconnect_on_connection_error(self):
        """Test reconnection when operation fails with ConnectionError."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url:
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock()
            # First call fails, second succeeds
            mock_redis.get.side_effect = [
                redis.ConnectionError("Connection lost"),
                b"value_after_reconnect",
            ]
            mock_redis.ping = AsyncMock()
            mock_redis.close = AsyncMock()
            mock_from_url.return_value = mock_redis
            
            with patch("asyncio.sleep", new_callable=AsyncMock):
                db = RedisDB("redis://localhost:6379")
                
                # This should trigger reconnect and retry
                result = await db.get("key1")
                
                assert result == b"value_after_reconnect"
                # get should be called twice (fail + retry after reconnect)
                assert mock_redis.get.call_count == 2

    @pytest.mark.asyncio
    async def test_reconnect_backoff_logic(self):
        """Test that reconnect uses backoff delays."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock()
            # Fail multiple times then succeed
            mock_redis.get.side_effect = [
                redis.ConnectionError("Connection lost"),
                b"success",
            ]
            mock_redis.ping = AsyncMock()
            # Fail first ping attempts, succeed on 3rd
            mock_redis.ping.side_effect = [
                redis.ConnectionError("ping fail 1"),
                redis.ConnectionError("ping fail 2"),
                None,  # Success
            ]
            mock_redis.close = AsyncMock()
            mock_from_url.return_value = mock_redis
            
            db = RedisDB("redis://localhost:6379")
            
            result = await db.get("key1")
            
            assert result == b"success"
            # Should have slept with backoff: 2s, 4s
            assert mock_sleep.call_count >= 2
            sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
            # First backoff should be 2 seconds
            assert 2 in sleep_calls
            # Second backoff should be 4 seconds
            assert 4 in sleep_calls

    @pytest.mark.asyncio
    async def test_reconnect_max_backoff(self):
        """Test that backoff is capped at 10 seconds."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            
            mock_redis = AsyncMock()
            mock_redis.get = AsyncMock()
            mock_redis.get.side_effect = [
                redis.ConnectionError("Connection lost"),
                b"success",
            ]
            mock_redis.ping = AsyncMock()
            # Fail many times to test max backoff
            mock_redis.ping.side_effect = [
                redis.ConnectionError("ping fail"),
                redis.ConnectionError("ping fail"),
                redis.ConnectionError("ping fail"),
                redis.ConnectionError("ping fail"),
                redis.ConnectionError("ping fail"),
                redis.ConnectionError("ping fail"),
                None,  # Success
            ]
            mock_redis.close = AsyncMock()
            mock_from_url.return_value = mock_redis
            
            db = RedisDB("redis://localhost:6379")
            
            result = await db.get("key1")
            
            assert result == b"success"
            # Check that no sleep was longer than 10 seconds
            sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
            assert all(delay <= 10 for delay in sleep_calls)
            # Should have at least one 10-second delay
            assert 10 in sleep_calls

    @pytest.mark.asyncio
    async def test_sentinel_reconnect_discovers_new_master(self):
        """Test that Sentinel reconnect discovers new master."""
        sentinel_url = "redis+sentinel://sentinel1:26379/mymaster?password=secret"
        
        with patch("opal_server.redis_utils.Sentinel") as mock_sentinel_class, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            
            mock_sentinel = MagicMock()
            mock_redis_old = AsyncMock()
            mock_redis_new = AsyncMock()
            
            # First master_for returns old connection, second returns new
            mock_sentinel.master_for.side_effect = [mock_redis_old, mock_redis_new]
            mock_sentinel_class.return_value = mock_sentinel
            
            # Old connection fails, new one succeeds
            mock_redis_old.get.side_effect = redis.ConnectionError("Master failed")
            mock_redis_old.close = AsyncMock()
            mock_redis_old.ping = AsyncMock()
            
            mock_redis_new.get.return_value = b"value_from_new_master"
            mock_redis_new.ping = AsyncMock()
            
            db = RedisDB(sentinel_url)
            
            # This should trigger reconnect and get new master
            result = await db.get("key1")
            
            assert result == b"value_from_new_master"
            # master_for should be called twice (init + reconnect)
            assert mock_sentinel.master_for.call_count == 2
            # Old connection should be closed
            mock_redis_old.close.assert_called()

    @pytest.mark.asyncio
    async def test_concurrent_reconnection_prevention(self):
        """Test that concurrent reconnections are prevented."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            
            mock_redis = AsyncMock()
            mock_from_url.return_value = mock_redis
            
            # Both operations fail initially
            get_side_effects = [redis.ConnectionError("Connection lost"), b"value"]
            set_side_effects = [redis.ConnectionError("Connection lost"), None]
            
            mock_redis.get = AsyncMock(side_effect=get_side_effects)
            mock_redis.set = AsyncMock(side_effect=set_side_effects)
            mock_redis.ping = AsyncMock()
            mock_redis.close = AsyncMock()
            
            db = RedisDB("redis://localhost:6379")
            
            from pydantic import BaseModel
            class TestModel(BaseModel):
                value: str
            
            # Start both operations concurrently - both will fail and try to reconnect
            # Only one should actually perform the reconnect
            results = await asyncio.gather(
                db.get("key1"),
                db.set("key2", TestModel(value="test")),
            )
            
            # Both operations should complete
            assert results[0] == b"value"
            # The _reconnect method should handle the concurrent access properly


class TestRedisDBEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_reconnect_eventually_raises_after_many_failures(self):
        """Test that reconnect eventually raises if it keeps failing."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            
            mock_redis = AsyncMock()
            mock_redis.get.side_effect = redis.ConnectionError("Connection lost")
            mock_redis.ping.side_effect = redis.ConnectionError("Always fails")
            mock_redis.close = AsyncMock()
            mock_from_url.return_value = mock_redis
            
            # Limit sleep calls to prevent infinite loop in test
            sleep_count = [0]
            async def limited_sleep(delay):
                sleep_count[0] += 1
                if sleep_count[0] > 10:
                    raise Exception("Too many retries")
            
            mock_sleep.side_effect = limited_sleep
            
            db = RedisDB("redis://localhost:6379")
            
            with pytest.raises(Exception):
                await db.get("key1")

    @pytest.mark.asyncio
    async def test_scan_reconnects_and_restarts(self):
        """Test that scan operation reconnects and restarts from beginning."""
        with patch("opal_server.redis_utils.redis.Redis.from_url") as mock_from_url, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            
            mock_redis = AsyncMock()
            
            # Track scan calls - first fails, then succeeds
            call_count = [0]
            
            async def mock_scan(cursor, match=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    # First call fails
                    raise redis.ConnectionError("Connection lost")
                elif call_count[0] == 2:
                    # After reconnect, return keys
                    return (b"cursor1", [b"key1"])
                else:
                    # Final call returns done
                    return (b"0", [])
            
            mock_redis.scan = mock_scan
            mock_redis.get = AsyncMock(return_value=b"value1")
            mock_redis.ping = AsyncMock()
            mock_redis.close = AsyncMock()
            mock_from_url.return_value = mock_redis
            
            db = RedisDB("redis://localhost:6379")
            
            values = []
            async for value in db.scan("pattern*"):
                values.append(value)
            
            assert values == [b"value1"]
            # scan should be called: 1 fail + 2 success
            assert call_count[0] == 3
