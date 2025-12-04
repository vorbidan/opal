# Bug Fix: Scope Prefix Mismatch in Pub/Sub Data Updates

## Bug Summary

**Status**: 🐛 CRITICAL - Data updates via pub/sub are silently failing in scoped deployments

**Impact**: When OPAL server publishes data updates to scoped clients via Redis Pub/Sub, the clients receive the events but skip processing them due to topic validation failure.

**Root Cause**: Server adds scope prefix to Redis channel destinations but does NOT update the `entry.topics` field in the payload, causing a mismatch between:
- Redis channel: `service1:data:policy_data` (has scope prefix)
- Payload topics: `["data:policy_data"]` (missing scope prefix)

**Evidence**:
```bash
# Client logs show validation failures
docker logs opal-client-service1 2>&1 | grep "has no topics matching"
# Output:
Data entry ... topics=['data:policy_data'] ... has no topics matching the data topics, skipping
```

## Current Broken Flow

```
1. Scope API receives update with topics: ["policy_data"]
   File: packages/opal-server/opal_server/scopes/api.py:350
   
2. API adds "data:" prefix to entry.topics
   entry.topics = ["data:policy_data"]
   
3. DataUpdatePublisher expands hierarchies (no change if no hierarchy)
   File: packages/opal-server/opal_server/data/data_update_publisher.py:98
   entry.topics = ["data:policy_data"]
   
4. ScopedServerSideTopicPublisher adds scope prefix to CHANNELS ONLY
   File: packages/opal-common/opal_common/topics/publisher.py:207
   scoped_topics = ["service1:data:policy_data"]  # For Redis channels
   entry.topics = ["data:policy_data"]  # Payload unchanged! ❌
   
5. Published to Redis
   Channel: "service1:data:policy_data" ✅
   Payload: {"entries": [{"topics": ["data:policy_data"], ...}]} ❌
   
6. Client receives event and validates
   File: packages/opal-client/opal_client/data/updater.py:470-481
   Client subscribed: ["service1:data:policy_data"]
   Payload topics: ["data:policy_data"]
   Validation: set().isdisjoint() returns True (no overlap)
   Result: Data update SKIPPED ❌
```

## The Fix: Consolidate Payload Manipulation

### Design Principle

**Move ALL payload topic transformations into ONE logical unit: `DataUpdatePublisher`**

This includes:
1. Adding `"data:"` prefix (currently in `scopes/api.py`)
2. Adding scope prefix (currently missing)
3. Expanding hierarchies (already in `DataUpdatePublisher`)

### Why This Approach?

✅ **Single Responsibility**: All topic transformations in one place
✅ **Clean Separation**: Publisher only handles routing, not data manipulation
✅ **Maintainable**: One place to understand and modify topic logic
✅ **Testable**: Easy to unit test all transformations together
✅ **No Payload Pollution**: Keeps publisher layer clean

### Implementation Steps

#### Step 1: Update `DataUpdatePublisher` Constructor

**File**: `packages/opal-server/opal_server/data/data_update_publisher.py`

**Current Code** (line ~18):
```python
class DataUpdatePublisher:
    def __init__(self, publisher: TopicPublisher) -> None:
        self._publisher = publisher
```

**Fixed Code**:
```python
class DataUpdatePublisher:
    def __init__(self, publisher: TopicPublisher) -> None:
        """Initialize DataUpdatePublisher.
        
        Args:
            publisher: The topic publisher to use for publishing updates.
                      If publisher has a _scope_id attribute (ScopedServerSideTopicPublisher),
                      topics will be prefixed with "data:" and "{scope_id}:" for proper
                      isolation in multi-tenant deployments.
        """
        self._publisher = publisher
        # Introspect publisher to check if it's scoped
        self._scope_id = getattr(publisher, '_scope_id', None)
```

#### Step 2: Update `publish_data_updates()` Method

**File**: `packages/opal-server/opal_server/data/data_update_publisher.py`

**Current Code** (lines ~85-98):
```python
# Expand the topics for each event to include sub topic combos
for entry in update.entries:
    topic_combos = []
    if entry.topics:
        for topic in entry.topics:
            topic_combos.extend(DataUpdatePublisher.get_topic_combos(topic))
        entry.topics = topic_combos  # Update entry with the exhaustive list
        all_topic_combos.update(topic_combos)
    else:
        logger.warning(
            "[{pid}] No topics were provided for the following entry: {entry}",
            pid=os.getpid(),
            entry=entry,
        )
```

**Fixed Code**:
```python
# Transform topics for each entry: add prefixes, expand hierarchies
for entry in update.entries:
    topic_combos = []
    if entry.topics:
        for topic in entry.topics:
            # Add both prefixes BEFORE expanding hierarchies
            if self._scope_id:
                # Add "data:" prefix if not present
                if not topic.startswith("data:"):
                    topic = f"data:{topic}"
                # Add scope prefix
                topic = f"{self._scope_id}:{topic}"
            
            # Now expand hierarchies with all prefixes already applied
            # e.g., "service1:data:policy_data/users" -> 
            #       ["service1:data:policy_data", "service1:data:policy_data/users"]
            topic_combos.extend(DataUpdatePublisher.get_topic_combos(topic))
        
        # Update entry with fully transformed topics
        entry.topics = topic_combos
        all_topic_combos.update(topic_combos)
    else:
        logger.warning(
            "[{pid}] No topics were provided for the following entry: {entry}",
            pid=os.getpid(),
            entry=entry,
        )
```

#### Step 3: Update Scopes API - Remove Duplicate Prefix Logic

**File**: `packages/opal-server/opal_server/scopes/api.py`

**Current Code** (lines ~348-354):
```python
@router.post("/{scope_id}/data/update")
async def publish_data_update_event(
    update: DataUpdate,
    claims: JWTClaims = Depends(authenticator),
    scope_id: str = Path(..., description="Scope ID"),
):
    try:
        require_peer_type(authenticator, claims, PeerType.datasource)
        restrict_optional_topics_to_publish(authenticator, claims, update)

        for entry in update.entries:
            entry.topics = [f"data:{topic}" for topic in entry.topics]

        await DataUpdatePublisher(
            ScopedServerSideTopicPublisher(pubsub_endpoint, scope_id)
        ).publish_data_updates(update)
    except Unauthorized as ex:
        logger.error(f"Unauthorized to publish update: {repr(ex)}")
        raise
```

**Fixed Code**:
```python
@router.post("/{scope_id}/data/update")
async def publish_data_update_event(
    update: DataUpdate,
    claims: JWTClaims = Depends(authenticator),
    scope_id: str = Path(..., description="Scope ID"),
):
    try:
        require_peer_type(authenticator, claims, PeerType.datasource)
        restrict_optional_topics_to_publish(authenticator, claims, update)

        # REMOVED: Don't add "data:" prefix here - let DataUpdatePublisher handle it
        # DataUpdatePublisher will introspect the publisher and detect it's scoped
        # for entry in update.entries:
        #     entry.topics = [f"data:{topic}" for topic in entry.topics]

        await DataUpdatePublisher(
            ScopedServerSideTopicPublisher(pubsub_endpoint, scope_id)
        ).publish_data_updates(update)
    except Unauthorized as ex:
        logger.error(f"Unauthorized to publish update: {repr(ex)}")
        raise
```

#### Step 4: Update Non-Scoped Usage (Maintain Backward Compatibility)

**File**: `packages/opal-server/opal_server/server.py`

**Current Code** (line ~226):
```python
if self.publisher is not None:
    data_update_publisher = DataUpdatePublisher(self.publisher)
```

**Fixed Code** (no change needed - scope_id defaults to None):
```python
if self.publisher is not None:
    # No scope_id passed - default non-scoped behavior maintained
    data_update_publisher = DataUpdatePublisher(self.publisher)
```

### Expected Flow After Fix

```
1. Scope API receives update with topics: ["policy_data"]
   
2. DataUpdatePublisher created with ScopedServerSideTopicPublisher
   
3. DataUpdatePublisher introspects publisher and detects _scope_id="service1"
   
4. DataUpdatePublisher transforms topics:
   a. Add "data:" prefix: "data:policy_data"
   b. Add scope prefix: "service1:data:policy_data"
   c. Expand hierarchies: ["service1:data:policy_data"] (no hierarchy in this case)
   d. Update entry.topics = ["service1:data:policy_data"] ✅
   
5. ScopedServerSideTopicPublisher adds scope prefix to channels
   scoped_topics = ["service1:data:policy_data"]
   
6. Published to Redis
   Channel: "service1:data:policy_data" ✅
   Payload: {"entries": [{"topics": ["service1:data:policy_data"], ...}]} ✅
   
7. Client validates
   Client subscribed: ["service1:data:policy_data"]
   Payload topics: ["service1:data:policy_data"]
   Validation: set().isdisjoint() returns False (topics match!)
   Result: Data update PROCESSED ✅
```

## Testing the Fix

### 1. Unit Tests

**File**: `packages/opal-server/opal_server/data/tests/test_data_update_publisher.py`

Add tests for scoped topic transformation:

```python
def test_scoped_topic_transformation():
    """Test that DataUpdatePublisher adds both data: and scope: prefixes."""
    from unittest.mock import Mock
    from opal_common.schemas.data import DataUpdate, DataSourceEntry
    
    # Mock scoped publisher with _scope_id attribute
    publisher = Mock()
    publisher._scope_id = "service1"  # Simulate ScopedServerSideTopicPublisher
    publisher.publish = Mock()
    
    # Create publisher - it will introspect and detect scope
    scoped_publisher = DataUpdatePublisher(publisher)
    
    # Create update with simple topic
    entry = DataSourceEntry(
        url="inline://",
        topics=["policy_data"],
        dst_path="/test"
    )
    update = DataUpdate(entries=[entry])
    
    # Publish
    await scoped_publisher.publish_data_updates(update)
    
    # Verify entry.topics was transformed
    assert entry.topics == ["service1:data:policy_data"]
    
    # Verify publisher was called with scoped topics
    publisher.publish.assert_called_once()
    call_args = publisher.publish.call_args
    assert "service1:data:policy_data" in call_args[0][0]


def test_non_scoped_topic_transformation():
    """Test that DataUpdatePublisher doesn't add prefixes for non-scoped publishers."""
    from unittest.mock import Mock
    from opal_common.schemas.data import DataUpdate, DataSourceEntry
    
    # Mock non-scoped publisher (no _scope_id attribute)
    publisher = Mock()
    publisher.publish = Mock()
    
    # Create publisher - introspection will find no _scope_id
    non_scoped_publisher = DataUpdatePublisher(publisher)
    
    # Create update with simple topic
    entry = DataSourceEntry(
        url="inline://",
        topics=["policy_data"],
        dst_path="/test"
    )
    update = DataUpdate(entries=[entry])
    
    # Publish
    await non_scoped_publisher.publish_data_updates(update)
    
    # Verify entry.topics was NOT transformed (no prefixes added)
    assert entry.topics == ["policy_data"]
    
    # Verify publisher was called with original topics
    publisher.publish.assert_called_once()
    call_args = publisher.publish.call_args
    assert "policy_data" in call_args[0][0]
```

### 2. Integration Test

**Setup**:
```bash
cd zscopes
docker-compose -f scopes-deployment-template.yml up -d
./init-scopes.sh
```

**Verify Before Fix**:
```bash
# Should see validation failures
docker logs opal-client-service1 2>&1 | grep "has no topics matching"
# Output: Data entry ... topics=['data:policy_data'] ... skipping
```

**Verify After Fix**:
```bash
# Should see NO validation failures
docker logs opal-client-service1 2>&1 | grep "has no topics matching"
# Output: (empty - no skipped entries)

# Should see successful data updates
docker logs opal-client-service1 2>&1 | grep "Updating policy data"
# Output: Updating policy data, reason: scope data update

# Verify data in OPA
curl localhost:8181/v1/data/services/service1
# Should return the actual data, not empty
```

### 3. Manual Testing

**Test Case 1: Simple Topic**
```bash
curl -X POST http://localhost:7002/scopes/service1/data/update \
  -H "Content-Type: application/json" \
  -d '{
    "entries": [{
      "topics": ["policy_data"],
      "url": "inline://",
      "data": {"test": "value"},
      "dst_path": "/test"
    }]
  }'

# Check client received and processed it
docker logs opal-client-service1 2>&1 | tail -20
# Should see: Updating policy data, reason: ...
# Should NOT see: has no topics matching
```

**Test Case 2: Hierarchical Topic**
```bash
curl -X POST http://localhost:7002/scopes/service1/data/update \
  -H "Content-Type: application/json" \
  -d '{
    "entries": [{
      "topics": ["policy_data/users/permissions"],
      "url": "inline://",
      "data": {"admin": ["read", "write"]},
      "dst_path": "/permissions"
    }]
  }'

# Transformation steps:
# 1. Original: "policy_data/users/permissions"
# 2. Add "data:" prefix: "data:policy_data/users/permissions"
# 3. Add scope prefix: "service1:data:policy_data/users/permissions"
# 4. Expand hierarchies (get_topic_combos preserves "service1:data:" prefix):
#    ["service1:data:policy_data", 
#     "service1:data:policy_data/users", 
#     "service1:data:policy_data/users/permissions"]
#
# Entry.topics will contain all three expanded topics with full prefixes
```

## Rollback Plan

If the fix causes issues:

1. **Revert Step 3 first** (scopes/api.py):
   ```python
   # Restore the old line:
   for entry in update.entries:
       entry.topics = [f"data:{topic}" for topic in entry.topics]
   ```

2. **Revert Step 2** (data_update_publisher.py):
   ```python
   # Restore original loop without scope_id logic
   for entry in update.entries:
       topic_combos = []
       if entry.topics:
           for topic in entry.topics:
               topic_combos.extend(DataUpdatePublisher.get_topic_combos(topic))
           entry.topics = topic_combos
           all_topic_combos.update(topic_combos)
   ```

3. **Revert Step 1** (data_update_publisher.py):
   ```python
   def __init__(self, publisher: TopicPublisher) -> None:
       self._publisher = publisher
   ```

## Notes

### Why Not Fix in Publisher?

**Rejected Approach**: Modify `ScopedServerSideTopicPublisher.publish()` to update payload

**Problems**:
1. ❌ Pollutes transport layer with data manipulation logic
2. ❌ Publisher would need to parse and modify complex payloads
3. ❌ Violates separation of concerns
4. ❌ Makes testing harder
5. ❌ Requires payload structure knowledge in publisher

**Our Approach is Better**:
1. ✅ All topic transformations in one logical place
2. ✅ Publisher stays focused on routing
3. ✅ Clear separation: data layer vs transport layer
4. ✅ Single source of truth for topic naming
5. ✅ Easier to test and maintain

### Backward Compatibility

The fix maintains backward compatibility:
- **Non-scoped deployments**: Publisher has no `_scope_id` attribute, `getattr()` returns `None`, no prefixes added
- **Scoped deployments**: Publisher has `_scope_id` attribute, both prefixes added automatically
- **No signature changes**: `DataUpdatePublisher` constructor signature unchanged
- **No changes to calling code**: API code remains the same (except removing duplicate prefix logic)
- No changes needed to client code
- No changes to pub/sub protocol

### Why Introspection?

**Advantages of using `getattr(publisher, '_scope_id', None)`:**

1. ✅ **No signature changes**: Maintains backward compatibility at the API level
2. ✅ **Automatic detection**: Publisher type determines behavior automatically
3. ✅ **Duck typing**: Pythonic approach - "if it has _scope_id, it's scoped"
4. ✅ **No cascading changes**: Doesn't require updating all DataUpdatePublisher instantiations
5. ✅ **Clear coupling**: DataUpdatePublisher behavior directly tied to publisher type

**Why this is safe:**
- `ScopedServerSideTopicPublisher` always has `_scope_id` attribute (set in `__init__`)
- Non-scoped publishers never have this attribute
- `getattr()` with default safely handles both cases
- No risk of `AttributeError`

### Future Enhancements

After this fix, consider:
1. Add validation that entry.topics match subscribed topics on server side
2. Add debug logging to show topic transformation steps
3. Consider making topic transformation more explicit/configurable
4. Document topic naming conventions clearly
