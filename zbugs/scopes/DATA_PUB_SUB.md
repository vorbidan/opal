# Data Pub/Sub Flow Documentation

This document provides evidence-based documentation of how OPAL's data pub/sub system works, with focus on topic transformation and client validation.

## Overview

OPAL uses Redis Pub/Sub for real-time data updates between server and clients. The system implements:
- Server-side topic transformation (scope prefixing)
- Client-side topic subscription
- Double validation (pub/sub layer + application layer)

## Topic Transformation Flow

### From Scope Configuration to Client Subscription

**Input**: Scope configuration with simple topic name
```json
{
  "scope_id": "service1",
  "data": {
    "entries": [{
      "topics": ["policy_data"],
      "url": "inline://",
      "dst_path": "/services/service1"
    }]
  }
}
```

**Step 1**: Scopes API adds "data:" prefix to entry.topics field
- **File**: `packages/opal-server/opal_server/scopes/api.py` line 350
- **Code**:
  ```python
  for entry in update.entries:
      entry.topics = [f"data:{topic}" for topic in entry.topics]
  ```
- **What happens**: Modifies the `topics` field **inside each entry** of `update.entries`
- **Result**: Entry's topics field: `["policy_data"]` → `["data:policy_data"]`
- **Update object state**: 
  ```python
  update.entries[0].topics = ["data:policy_data"]
  ```

**Step 2**: ScopedServerSideTopicPublisher determines destination Redis channels
- **File**: `packages/opal-common/opal_common/topics/publisher.py` line 207
- **Code**:
  ```python
  scoped_topics = [f"{self._scope_id}:{topic}" for topic in topics]
  ```
- **What happens**: 
  - Creates NEW list of destination channels by prefixing with scope ID
  - The `topics` parameter comes from `all_topic_combos` collected from all entries in DataUpdatePublisher
  - **Does NOT modify the update.entries[].topics field** ⚠️ **BUG: This is the problem!**
  - Only determines which Redis Pub/Sub channels to publish to
- **Source of topics**: Derived from `update.entries[].topics` which at this point contains `["data:policy_data"]`
- **Result**: 
  - **Redis channels to publish to**: `["service1:data:policy_data"]`
  - **update.entries[0].topics remains**: `["data:policy_data"]` (unchanged - THIS IS THE BUG!)

**🐛 BUG IDENTIFIED**: At this stage, a critical mismatch is created:
- Redis channel being published to: `service1:data:policy_data`
- Payload's entry.topics field: `["data:policy_data"]`
- **This mismatch is never resolved**, causing client validation to fail and skip the data update!

**Evidence from logs**:
```
Data entry ... topics=['data:policy_data'] ... has no topics matching the data topics, skipping
```

Client subscribes to `['service1:data:policy_data']` but receives payload with `topics=['data:policy_data']`, so the set intersection check fails.

**Step 3**: DataUpdatePublisher expands topic hierarchies (NOT scope prefixes!)
- **File**: `packages/opal-server/opal_server/data/data_update_publisher.py` lines 91-98
- **Code**:
  ```python
  for topic in entry.topics:
      topic_combos.extend(DataUpdatePublisher.get_topic_combos(topic))
  entry.topics = topic_combos  # Update entry with the exhaustive list
  ```
- **What happens**: 
  - Expands hierarchical topics like `"data:policy_data/users/keys"` into:
    - `["data:policy_data", "data:policy_data/users", "data:policy_data/users/keys"]`
  - Preserves any prefix before the colon (`:`)
  - **Does NOT add scope prefix** - that was already done in Step 2 for destination channels only
- **In our case**: `["data:policy_data"]` has no hierarchy to expand
- **Result**: Entry.topics remains `["data:policy_data"]` (unchanged - no hierarchy present)
- **Update object state**:
  ```python
  update.entries[0].topics = ["data:policy_data"]  # Still no scope prefix!
  ```

**Step 4**: Published to Redis - **MISMATCH CAUSES DATA UPDATE FAILURE**
- **Redis Channel (from Step 2)**: `service1:data:policy_data`
- **Payload entry.topics (from Step 3)**: `["data:policy_data"]`
- **Result**: Client receives event on subscribed channel but validation fails:
  ```python
  # Client validation logic (updater.py:470-481)
  entry.topics = ["data:policy_data"]  # From payload
  self._data_topics = ["service1:data:policy_data"]  # Client subscribed
  
  set(["data:policy_data"]).isdisjoint(set(["service1:data:policy_data"]))
  # Returns True (no overlap) → Data update is SKIPPED!
  ```
- **Log Evidence**:
  ```
  Data entry ... topics=['data:policy_data'] ... has no topics matching the data topics, skipping
  ```

## THE BUG

**Root Cause**: `ScopedServerSideTopicPublisher` adds scope prefix to Redis channels but does NOT update the `entry.topics` field in the payload.

**Impact**: Data updates via pub/sub are silently skipped by clients due to validation failure.

**The Fix**: Update `entry.topics` when adding scope prefix to channels.

**Location to Fix**: `packages/opal-common/opal_common/topics/publisher.py` line ~207

**Current Code**:
```python
async def publish(
    self, topics: List[str], data: Any, sync: bool = True
) -> asyncio.Task:
    """Publish to scoped topics."""
    scoped_topics = [f"{self._scope_id}:{topic}" for topic in topics]
    return await super().publish(scoped_topics, data, sync)
```

**Proposed Fix**:
```python
async def publish(
    self, topics: List[str], data: Any, sync: bool = True
) -> asyncio.Task:
    """Publish to scoped topics."""
    scoped_topics = [f"{self._scope_id}:{topic}" for topic in topics]
    
    # FIX: Also update entry.topics in the payload to match scoped channels
    if isinstance(data, dict) and "entries" in data:
        for entry in data.get("entries", []):
            if "topics" in entry:
                entry["topics"] = [f"{self._scope_id}:{topic}" for topic in entry["topics"]]
    
    return await super().publish(scoped_topics, data, sync)
```

This ensures:
1. Redis channels have scope prefix: `service1:data:policy_data`
2. Payload entry.topics also has scope prefix: `["service1:data:policy_data"]`
3. Client validation succeeds: both sets overlap!
4. Data updates are processed correctly ✅

## Client Subscription

### Client-Side Topic Namespacing

**File**: `packages/opal-client/opal_client/data/updater.py` lines 100-113

```python
self._scope_id = opal_client_config.SCOPE_ID
self._data_topics = (
    data_topics if data_topics is not None else opal_client_config.DATA_TOPICS
)

if self._scope_id == "default":
    # Legacy mode - topics used as-is
    data_sources_config_url: str = (
        data_sources_config_url
        or opal_client_config.DEFAULT_DATA_SOURCES_CONFIG_URL
    )
else:
    # Scoped mode - namespace topics with scope prefix
    data_sources_config_url = (
        f"{opal_client_config.SERVER_URL}/scopes/{self._scope_id}/data"
    )
    # Namespacing the data topics for the specific scope
    self._data_topics = [
        f"{self._scope_id}:data:{topic}" for topic in self._data_topics
    ]
```

**Client Configuration**:
```yaml
OPAL_SCOPE_ID: service1
OPAL_DATA_TOPICS: policy_data
```

**Client subscribes to**: `["service1:data:policy_data"]`

### Subscription Logging

**File**: `packages/opal-client/opal_client/data/updater.py` line 345

```python
logger.info("Subscribing to topics: {topics}", topics=self._data_topics)
```

**Log Output**:
```
Subscribing to topics: ['service1:data:policy_data']
```

### Event Reception and Logging

**File**: `packages/opal-client/opal_client/data/updater.py` lines 177-191

When client receives an event on a subscribed topic:

```python
async def _update_policy_data_callback(self, data: Optional[dict] = None, topic=""):
    """Callback invoked by the Pub/Sub client whenever a data update is
    published on one of our subscribed topics.
    """
    if data is not None:
        reason = data.get("reason", "")
    else:
        reason = "Periodic update"

    logger.info("Updating policy data, reason: {reason}", reason=reason)
    update = DataUpdate.parse_obj(data)
    await self.trigger_data_update(update)
```

**Log Output**:
```
Updating policy data, reason: scope data update
Triggering data update with id: 3f8a9b2c...
```

## Double Validation: Pub/Sub Layer + Application Layer

### Layer 1: Pub/Sub Subscription (Redis)

Client subscribes to specific Redis channels via PubSubClient:
- Only receives messages published to subscribed channels
- Redis handles routing based on exact channel match
- Example: Client subscribed to `service1:data:policy_data` will NOT receive messages on `service2:data:policy_data`

### Layer 2: Application Layer Validation

**CRITICAL EVIDENCE**: Even after receiving an event on a subscribed topic, the client validates each entry's topics field.

**But wait - there's a mismatch causing failure!**
- Client subscribes to: `service1:data:policy_data`
- Payload contains entry.topics: `["data:policy_data"]`
- **These don't match!** Validation fails and data is skipped.

**The Root Cause**: Server adds scope prefix to Redis channels but NOT to payload entry.topics.

**File**: `packages/opal-client/opal_client/data/updater.py` lines 470-481

```python
for entry in update.entries:
    if not entry.topics:
        logger.debug("Data entry {entry} has no topics, skipping", entry=entry)
        continue

    # Only process entries that match one of our subscribed data topics
    if set(entry.topics).isdisjoint(set(self._data_topics)):
        logger.debug(
            "Data entry {entry} has no topics matching the data topics, skipping",
            entry=entry,
        )
        continue

    # Process entry...
```

**Validation Failure**:
```python
entry.topics = ["data:policy_data"]  # From server payload (BUG!)
self._data_topics = ["service1:data:policy_data"]  # Client subscribed topics

set(["data:policy_data"]).isdisjoint(set(["service1:data:policy_data"]))
# Returns True (no overlap!) - Entry is SKIPPED! ❌
```

**Log Evidence**:
```
Data entry ... topics=['data:policy_data'] ... has no topics matching the data topics, skipping
```

### Why This Matters

**Double validation ensures complete data isolation:**

1. **Pub/Sub Layer**: Client only receives events on channels it subscribed to
2. **Application Layer**: Client validates `entry.topics` in payload matches `self._data_topics`

**Example Scenario**:

**Client Configuration**:
```yaml
OPAL_SCOPE_ID: service1
OPAL_DATA_TOPICS: policy_data
```

**Client subscribes to**: `["service1:data:policy_data"]`

**Receives payload**:
```json
{
  "reason": "scope data update",
  "entries": [
    {
      "topics": ["service1:data:policy_data"],
      "url": "inline://",
      "data": {...},
      "dst_path": "/services/service1"
    }
  ]
}
```

**Validation Logic**:
```python
# entry.topics = ["service1:data:policy_data"]
# self._data_topics = ["service1:data:policy_data"]

set(["service1:data:policy_data"]).isdisjoint(set(["service1:data:policy_data"]))
# Returns False (sets have overlap!)
# → Entry is PROCESSED ✅
```

**If entry had wrong topics**:
```python
# entry.topics = ["service2:data:policy_data"]
# self._data_topics = ["service1:data:policy_data"]

set(["service2:data:policy_data"]).isdisjoint(set(["service1:data:policy_data"]))
# Returns True (no overlap!)
# → Entry is SKIPPED ❌
```

### Implications for Scope Isolation

**CURRENT STATE: BROKEN** 🐛

The double validation mechanism is theoretically sound but has a critical bug:

1. ❌ **Pub/Sub Layer Works**: Client receives events on subscribed channels
2. ❌ **Application Layer Fails**: Client validation rejects all entries due to topic mismatch
3. ❌ **Result**: Data updates are silently skipped, system appears to work but data is never updated

**After Fix (proposed above)**:

1. ✅ **Complete Isolation**: Server adds scope prefix to both channels AND payload topics
2. ✅ **Multi-Entry Safety**: A single DataUpdate can contain entries for multiple scopes; each client only processes its own
3. ✅ **Pattern Matching Ready**: Future gateway pattern implementation can leverage this to filter entries
4. ✅ **Defense in Depth**: Two independent layers of validation prevent data leakage

### Debug Commands

To verify client subscription and event reception:

```bash
# Check what topics client subscribed to
docker logs opal-client-service1 2>&1 | grep "Subscribing to topics"
# Expected: Subscribing to topics: ['service1:data:policy_data']

# Check when client receives events
docker logs opal-client-service1 2>&1 | grep "Updating policy data"
# Expected: Updating policy data, reason: scope data update

# Check for topic mismatch (should not appear in isolated mode)
docker logs opal-client-service1 2>&1 | grep "has no topics matching"
# Expected: (no output - all entries match)
```

## Summary

**🐛 CRITICAL BUG IDENTIFIED**: Scoped pub/sub data updates are silently failing!

The complete flow from scope configuration to client validation:

```
Scope Config: topics: ["policy_data"]
       ↓
Step 1: Scopes API modifies entry.topics field
        entry.topics = ["data:policy_data"]
       ↓
Step 2: ScopedServerSideTopicPublisher determines Redis channels
        - Reads from entry.topics: ["data:policy_data"]
        - Calculates destination: ["service1:data:policy_data"]
        - ❌ BUG: Does NOT modify entry.topics in payload!
        - entry.topics still = ["data:policy_data"]
       ↓
Step 3: DataUpdatePublisher expands hierarchies (if any)
        - Only expands topic hierarchies like "a/b/c" → ["a", "a/b", "a/b/c"]
        - Preserves prefix before colon (:)
        - Does NOT add scope prefix (that should have been done in Step 2!)
        - entry.topics = ["data:policy_data"]  # No hierarchy, so unchanged
       ↓
Published to Redis:
  - Channel: "service1:data:policy_data" ✅
  - Payload: entry.topics = ["data:policy_data"] ❌ MISMATCH!
       ↓
Client (subscribed to "service1:data:policy_data") receives event ✅
       ↓
Client validates: entry.topics ∩ self._data_topics
       - entry.topics = ["data:policy_data"]
       - self._data_topics = ["service1:data:policy_data"]
       - set().isdisjoint() returns True (no overlap!)
       - Result: Data update SKIPPED ❌
       ↓
Log: "Data entry ... has no topics matching the data topics, skipping"
```

**The Fix**: Modify `ScopedServerSideTopicPublisher.publish()` to update `entry.topics` in the payload when adding scope prefix to channels. See "THE BUG" section above for implementation.
