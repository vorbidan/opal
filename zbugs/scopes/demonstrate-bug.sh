#!/bin/bash

# Bug Demonstration Script: Scope Prefix Mismatch
# This script demonstrates the critical bug where scoped data updates are silently skipped
# due to topic mismatch between Redis channels and payload topics.

set -e  # Exit on error

# Configuration
OPAL_SERVER="http://localhost:7002"
OPA_ENDPOINT="http://localhost:8181"
SCOPE_ID="service1"

echo ""
echo "=========================================================================="
echo "🐛 OPAL Scopes Bug Demonstration: Topic Prefix Mismatch"
echo "=========================================================================="
echo ""
echo "This script demonstrates a critical bug where:"
echo "  1. Server publishes to Redis channel: service1:data:policy_data"
echo "  2. Payload contains topics field: [\"data:policy_data\"]"
echo "  3. Client receives event but skips it due to validation failure"
echo ""
echo "Expected: Client processes data updates"
echo "Actual:   Client silently discards all updates"
echo ""

# Function to wait for service
wait_for_service() {
    local url=$1
    local service_name=$2
    local max_attempts=30
    local attempt=1
    
    printf "⏳ Waiting for ${service_name} to be ready"
    while [ $attempt -le $max_attempts ]; do
        if curl -s "${url}" > /dev/null 2>&1; then
            printf " ✓\n"
            return 0
        fi
        printf "."
        sleep 1
        attempt=$((attempt + 1))
    done
    printf " ✗\n"
    echo "❌ ${service_name} failed to start after ${max_attempts} seconds"
    return 1
}

# Function to extract and display logs
show_event_flow() {
    local operation=$1
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📊 Event Flow Analysis: ${operation}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    echo ""
    echo "🔍 Server-Side Logs:"
    echo "─────────────────────────────────────────────────────────────────────"
    docker logs opal-server 2>&1 | tail -50 | grep -E "Publishing (to|data update)" | tail -10 || echo "  (No publishing logs found)"
    
    echo ""
    echo "🔍 Client-Side Logs:"
    echo "─────────────────────────────────────────────────────────────────────"
    
    echo ""
    echo "  📡 Subscription:"
    docker logs opal-client-service1 2>&1 | grep "Subscribing to topics" | tail -1 || echo "    (No subscription logs)"
    
    echo ""
    echo "  📨 Events Received:"
    docker logs opal-client-service1 2>&1 | grep -E "Received notification|Broadcasting incoming" | tail -5 || echo "    (No events received)"
    
    echo ""
    echo "  ⚙️  Processing Attempts:"
    docker logs opal-client-service1 2>&1 | grep "Updating policy data" | tail -5 || echo "    (No processing logs)"
    
    echo ""
    echo "  ❌ Validation Failures (THE BUG):"
    docker logs opal-client-service1 2>&1 | grep "has no topics matching" | tail -5 || echo "    (No validation failures - bug not triggered)"
    
    echo ""
}

echo "=========================================================================="
echo "Step 1: Start Services"
echo "=========================================================================="
echo ""

# Check if services are already running
if docker ps | grep -q opal-server; then
    echo "⚠️  Services already running. Stopping..."
    docker compose down -v
    sleep 2
fi

echo "🚀 Starting OPAL stack..."
docker compose up -d

echo ""
wait_for_service "${OPAL_SERVER}" "OPAL Server" || exit 1
wait_for_service "${OPA_ENDPOINT}/v1/data" "OPA (embedded in client)" || exit 1

echo ""
echo "✅ All services are ready"

sleep 3  # Give services time to fully initialize

echo ""
echo "=========================================================================="
echo "Step 2: Verify Initial State (No Data in OPA)"
echo "=========================================================================="
echo ""

echo "📋 Checking OPA data store (should be empty)..."
OPA_DATA=$(curl -s "${OPA_ENDPOINT}/v1/data")
echo "${OPA_DATA}" | jq '.' || echo "${OPA_DATA}"

if echo "${OPA_DATA}" | jq -e '.result.services' > /dev/null 2>&1; then
    echo "⚠️  OPA already has data - this might affect the test"
else
    echo "✓ OPA data store for service1 is empty as expected"
fi

echo ""
echo "=========================================================================="
echo "Step 3: Create Scope with Initial Data"
echo "=========================================================================="
echo ""

SCOPE_PAYLOAD='{
  "scope_id": "service1",
  "policy": {
    "url": "https://github.com/permitio/opal-example-policy-repo",
    "branch": "master",
    "directories": ["."],
    "auth": {
        "auth_type": "none"
    },
    "source_type": "git"
  },
  "data": {
    "entries": [
      {
        "url": "inline://",
        "data": {
          "users": [
            {"id": "user1", "name": "Alice", "role": "admin", "service": "service1"},
            {"id": "user2", "name": "Bob", "role": "user", "service": "service1"}
          ],
          "roles": ["admin", "user", "viewer"],
          "permissions": {
            "admin": ["read", "write", "delete"],
            "user": ["read", "write"],
            "viewer": ["read"]
          }
        },
        "dst_path": "/services/service1",
        "topics": ["policy_data"]
      }
    ]
  }
}'

echo "📤 Creating scope: ${SCOPE_ID}"
echo "   Topics: [\"policy_data\"]"
echo "   Expected transformation: policy_data → service1:data:policy_data"
echo ""

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X PUT "${OPAL_SERVER}/scopes" \
  -H "Content-Type: application/json" \
  -d "${SCOPE_PAYLOAD}")

if [ "${HTTP_CODE}" = "201" ] || [ "${HTTP_CODE}" = "200" ]; then
    echo "✅ Scope created successfully (HTTP ${HTTP_CODE})"
else
    echo "❌ Failed to create scope (HTTP ${HTTP_CODE})"
    exit 1
fi

echo ""
echo "⏳ Waiting for client to process initial data (5 seconds)..."
sleep 5

show_event_flow "Scope Creation"

echo ""
echo "=========================================================================="
echo "Step 4: Verify Data NOT Delivered to OPA (Bug Demonstration)"
echo "=========================================================================="
echo ""

echo "🔎 Checking if data was delivered to OPA..."
OPA_DATA_AFTER=$(curl -s "${OPA_ENDPOINT}/v1/data/services/service1")
echo "${OPA_DATA_AFTER}" | jq '.' || echo "${OPA_DATA_AFTER}"

if echo "${OPA_DATA_AFTER}" | jq -e '.result.users' > /dev/null 2>&1; then
    echo "✅ Data WAS delivered (bug is fixed!)"
else
    echo "❌ Data was NOT delivered to OPA (BUG CONFIRMED!)"
    echo ""
    echo "Why did this fail?"
    echo "  1. Server published to Redis channel: service1:data:policy_data"
    echo "  2. Payload contains: topics=['data:policy_data'] (missing scope prefix!)"
    echo "  3. Client subscribed to: service1:data:policy_data"
    echo "  4. Client validation checks: set(['data:policy_data']).isdisjoint(set(['service1:data:policy_data']))"
    echo "  5. Validation returns True (no overlap) → Data update SKIPPED"
fi

echo ""
echo "=========================================================================="
echo "Step 5: Update Scope Data (Second Attempt)"
echo "=========================================================================="
echo ""

UPDATE_PAYLOAD='{
  "entries": [
    {
      "url": "inline://",
      "data": {
        "users": [
          {"id": "user1", "name": "Alice Updated", "role": "superadmin", "service": "service1"},
          {"id": "user3", "name": "Charlie", "role": "user", "service": "service1"}
        ],
        "updated_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"
      },
      "dst_path": "/services/service1",
      "topics": ["policy_data"]
    }
  ]
}'

echo "📤 Updating scope data via pub/sub..."
echo "   Topics: [\"policy_data\"]"
echo ""

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "${OPAL_SERVER}/scopes/${SCOPE_ID}/data/update" \
  -H "Content-Type: application/json" \
  -d "${UPDATE_PAYLOAD}")

if [ "${HTTP_CODE}" = "200" ]; then
    echo "✅ Update published successfully (HTTP ${HTTP_CODE})"
else
    echo "❌ Failed to publish update (HTTP ${HTTP_CODE})"
fi

echo ""
echo "⏳ Waiting for client to process update (5 seconds)..."
sleep 5

show_event_flow "Scope Data Update"

echo ""
echo "=========================================================================="
echo "Step 6: Verify Update NOT Delivered (Bug Confirmation)"
echo "=========================================================================="
echo ""

echo "🔎 Checking if update was delivered to OPA..."
OPA_DATA_FINAL=$(curl -s "${OPA_ENDPOINT}/v1/data/services/service1")
echo "${OPA_DATA_FINAL}" | jq '.' || echo "${OPA_DATA_FINAL}"

HAS_UPDATED=$(echo "${OPA_DATA_FINAL}" | jq -e '.result.updated_at' > /dev/null 2>&1 && echo "yes" || echo "no")

if [ "${HAS_UPDATED}" = "yes" ]; then
    echo "✅ Update WAS delivered (bug is fixed!)"
else
    echo "❌ Update was NOT delivered to OPA (BUG CONFIRMED AGAIN!)"
fi

echo ""
echo "=========================================================================="
echo "Step 7: Detailed Bug Analysis"
echo "=========================================================================="
echo ""

echo "🔬 Analyzing the root cause..."
echo ""

echo "1️⃣  Server-side topic transformation:"
echo "   ─────────────────────────────────────────"
docker logs opal-server 2>&1 | grep -A 2 "Publishing data update" | tail -5 || echo "   (No detailed server logs)"

echo ""
echo "2️⃣  What the client received (from pub/sub):"
echo "   ─────────────────────────────────────────"
docker logs opal-client-service1 2>&1 | grep -B 2 "has no topics matching" | tail -10 || echo "   (No validation failure logs - check if DEBUG logging is enabled)"

echo ""
echo "3️⃣  The mismatch:"
echo "   ─────────────────────────────────────────"
echo "   Redis Channel (where event was published):"
echo "      service1:data:policy_data  ✓"
echo ""
echo "   Payload topics field (what client validates):"
echo "      [\"data:policy_data\"]  ✗ (missing scope prefix!)"
echo ""
echo "   Client subscribed topics:"
echo "      [\"service1:data:policy_data\"]  ✓"
echo ""
echo "   Result: Client receives event but validation fails!"

echo ""
echo "=========================================================================="
echo "Summary"
echo "=========================================================================="
echo ""
echo "🐛 BUG CONFIRMED:"
echo "  • Server adds scope prefix to Redis channels but NOT to payload topics"
echo "  • Client receives events but discards them due to validation failure"
echo "  • Data updates are silently lost - no errors visible at INFO log level"
echo ""
echo "📋 Evidence:"
echo "  • OPA data store is empty (data never delivered)"
echo "  • Server logs show 'Publishing to topics: service1:data:policy_data'"
echo "  • Client logs show 'Subscribing to topics: service1:data:policy_data'"
echo "  • Client DEBUG logs show 'has no topics matching the data topics, skipping'"
echo ""
echo "✅ Fix:"
echo "  • Consolidate all topic transformations in DataUpdatePublisher"
echo "  • Add scope prefix to payload entry.topics, not just to channels"
echo "  • See: /zscopes/BUG_FIX_SCOPE_PREFIX_MISMATCH.md"
echo ""
echo "=========================================================================="
echo ""

echo "🧹 Cleanup: Would you like to stop the services? [y/N]"
read -t 10 -r CLEANUP_RESPONSE || CLEANUP_RESPONSE="n"
if [[ $CLEANUP_RESPONSE =~ ^[Yy]$ ]]; then
    echo "Stopping services..."
    docker compose down
    echo "✅ Services stopped"
else
    echo "Services left running for further inspection."
    echo "To view logs: docker compose logs -f"
    echo "To stop: docker compose down"
fi

echo ""
