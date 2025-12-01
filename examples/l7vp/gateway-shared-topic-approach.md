# Gateway Shared Topic Approach

## Overview

This approach allows a gateway OPAL client to receive data updates from multiple service scopes without needing wildcards or listing every scope topic explicitly. Each scope publishes to both its own scope-specific topic AND a shared `gateway_data` topic.

## Architecture

```
OPAL Server (Scopes Enabled)
├── Git Repo (Common): https://github.com/yourorg/policies
│   ├── utils.rego
│   ├── gateway/*.rego
│   └── services/app1/*.rego, services/app2/*.rego
│
├── Scope: app1
│   ├── Policy: Git repo, directories: ["."] (entire repo)
│   ├── Client Filter: POLICY_SUBSCRIPTION_DIRS=.:services/app1
│   ├── Data: /services/app1/*
│   └── Topics: ["app1:data:app1", "gateway_data"]  ← Publishes to both
│
├── Scope: app2
│   ├── Policy: Git repo, directories: ["."] (entire repo)
│   ├── Client Filter: POLICY_SUBSCRIPTION_DIRS=.:services/app2
│   ├── Data: /services/app2/*
│   └── Topics: ["app2:data:app2", "gateway_data"]  ← Publishes to both
│
└── Scope: app3
    ├── Policy: Git repo, directories: ["."] (entire repo)
    ├── Client Filter: POLICY_SUBSCRIPTION_DIRS=.:services/app3
    ├── Data: /services/app3/*
    └── Topics: ["app3:data:app3", "gateway_data"]  ← Publishes to both

Service Mesh Clients:
├── app1-sidecar: SCOPE_ID=app1, subscribes to "app1:data:app1" (isolated)
├── app2-sidecar: SCOPE_ID=app2, subscribes to "app2:data:app2" (isolated)
└── app3-sidecar: SCOPE_ID=app3, subscribes to "app3:data:app3" (isolated)

Gateway Client:
└── gateway-client: subscribes to "gateway_data" (aggregated), POLICY_SUBSCRIPTION_DIRS=.
```

## Policy Configuration Strategy

**Key Insight:** The scope's `policy` field is **required**, but you don't need to configure different Git repos or directories per scope. Instead:

1. **All scopes use the same Git repo** with `directories: ["."]` (entire repo)
2. **Clients control policy filtering** via `OPAL_POLICY_SUBSCRIPTION_DIRS`
3. **Avoids duplicating Git configuration** across scopes

This approach:
- ✅ Keeps scope configuration simple and consistent
- ✅ Clients decide what policies they need
- ✅ Single Git repo for all policies
- ✅ Easy to add new services (just copy scope config)

## Configuration

### OPAL Server Configuration

```yaml
opal-server:
  image: opal-server:latest
  environment:
    # Enable scopes
    - OPAL_SCOPES=true
    - OPAL_REDIS_URL=redis://redis:6379
    - OPAL_BROADCAST_URI=redis://redis:6379
    
    # Optional: Server-level policy repo (not used by scopes, but available for non-scoped clients)
    - OPAL_POLICY_REPO_URL=https://github.com/yourorg/policies
    - OPAL_POLICY_REPO_MAIN_BRANCH=main
    
    # Optional: Global config for gateway initial load
    - OPAL_DATA_CONFIG_SOURCES={"config":{"entries":[]}}
  
  ports:
    - "7002:7002"
```

**Note:** Each scope must still define its own `policy` configuration (Git URL, branch, directories) as it's a required field. However, all scopes can use the same Git repo with `directories: ["."]`, and clients control which policies they receive via `OPAL_POLICY_SUBSCRIPTION_DIRS`.

### Define Scopes with Dual Topics

Each scope is configured to publish data updates to TWO topics:
1. Its own scope-specific topic (for service mesh clients)
2. The shared `gateway_data` topic (for the gateway)

**Policy Configuration:** All scopes use the same Git repo with `directories: ["."]` to avoid duplication. Clients control which policies they receive using `OPAL_POLICY_SUBSCRIPTION_DIRS`.

#### App1 Scope

```bash
curl -X PUT http://opal-server:7002/scopes/app1 \
  -H "Content-Type: application/json" \
  -d '{
    "scope_id": "app1",
    "policy": {
      "source_type": "git",
      "url": "https://github.com/yourorg/policies",
      "branch": "main",
      "directories": ["."],
      "auth": {"auth_type": "none"}
    },
    "data": {
      "entries": [
        {
          "url": "https://jwks-provider/.well-known/jwks.json",
          "dst_path": "/shared/jwks",
          "topics": ["app1:data:app1", "gateway_data"],
          "periodic_update_interval": 3600
        },
        {
          "url": "https://api.internal/app1/users",
          "dst_path": "/services/app1/users",
          "topics": ["app1:data:app1", "gateway_data"],
          "periodic_update_interval": 60
        },
        {
          "url": "https://api.internal/app1/roles",
          "dst_path": "/services/app1/roles",
          "topics": ["app1:data:app1", "gateway_data"],
          "periodic_update_interval": 60
        }
      ]
    }
  }'
```

#### App2 Scope

```bash
curl -X PUT http://opal-server:7002/scopes/app2 \
  -H "Content-Type: application/json" \
  -d '{
    "scope_id": "app2",
    "policy": {
      "source_type": "git",
      "url": "https://github.com/yourorg/policies",
      "branch": "main",
      "directories": ["."],
      "auth": {"auth_type": "none"}
    },
    "data": {
      "entries": [
        {
          "url": "https://jwks-provider/.well-known/jwks.json",
          "dst_path": "/shared/jwks",
          "topics": ["app2:data:app2", "gateway_data"],
          "periodic_update_interval": 3600
        },
        {
          "url": "https://api.internal/app2/users",
          "dst_path": "/services/app2/users",
          "topics": ["app2:data:app2", "gateway_data"],
          "periodic_update_interval": 60
        },
        {
          "url": "https://api.internal/app2/permissions",
          "dst_path": "/services/app2/permissions",
          "topics": ["app2:data:app2", "gateway_data"],
          "periodic_update_interval": 60
        }
      ]
    }
  }'
```

### Service Mesh Client Configuration

Service mesh sidecars subscribe ONLY to their own scope-specific topic and use `POLICY_SUBSCRIPTION_DIRS` to filter which policies they receive:

```yaml
# App1 Sidecar
app1-sidecar:
  image: opal-client:latest
  environment:
    - OPAL_SERVER_URL=http://opal-server:7002
    - OPAL_SCOPE_ID=app1
    
    # Client decides which policy directories to subscribe to
    # Even though scope has all policies, client filters to just what it needs
    - OPAL_POLICY_SUBSCRIPTION_DIRS=.:services/app1
    
    - OPAL_INLINE_OPA_ENABLED=true
  network_mode: "service:app1"

# App2 Sidecar
app2-sidecar:
  image: opal-client:latest
  environment:
    - OPAL_SERVER_URL=http://opal-server:7002
    - OPAL_SCOPE_ID=app2
    
    # Each client subscribes to its own service directory
    - OPAL_POLICY_SUBSCRIPTION_DIRS=.:services/app2
    
    - OPAL_INLINE_OPA_ENABLED=true
  network_mode: "service:app2"
```

**Note:** The scope's `policy.directories` field is set to `["."]` (entire repo), but clients use `OPAL_POLICY_SUBSCRIPTION_DIRS` to filter which directories they actually receive. This avoids duplicating Git configuration per scope.

### Gateway Client Configuration

Gateway subscribes ONLY to the shared topic and gets ALL scope updates:

```yaml
gateway-client:
  image: opal-client:latest
  environment:
    # Connect to OPAL server
    - OPAL_SERVER_URL=http://opal-server:7002
    - OPAL_CLIENT_TOKEN=${GATEWAY_CLIENT_TOKEN}
    
    # No OPAL_SCOPE_ID - gateway is scopeless
    
    # Subscribe to ALL policies (entire repo)
    - OPAL_POLICY_SUBSCRIPTION_DIRS=.
    
    # Subscribe ONLY to the shared gateway topic
    - OPAL_DATA_TOPICS=gateway_data
    
    # Optional: Point to server's global config for initial load
    - OPAL_DEFAULT_DATA_SOURCES_CONFIG_URL=http://opal-server:7002/data/config
    
    # OPA configuration
    - OPAL_INLINE_OPA_ENABLED=false
    - OPAL_POLICY_STORE_URL=http://gateway-opa:8181
  
  ports:
    - "7766:7000"

gateway-opa:
  image: openpolicyagent/opa:latest
  command:
    - "run"
    - "--server"
    - "--addr=0.0.0.0:8181"
  ports:
    - "8181:8181"
```

## How It Works

### Data Flow

1. **Periodic Updates (Automatic):**
   - Each scope's data sources are polled at their configured `periodic_update_interval`
   - When data changes, updates are published to BOTH topics:
     - Scope-specific topic: `app1:data:app1`
     - Shared topic: `gateway_data`

2. **Service Mesh Clients:**
   - App1 sidecar receives updates on `app1:data:app1` topic only
   - App2 sidecar receives updates on `app2:data:app2` topic only
   - Each sees only its own service's data (isolated)

3. **Gateway Client:**
   - Receives updates on `gateway_data` topic
   - Gets ALL updates from ALL scopes
   - Data is mounted with paths defined in each scope (e.g., `/services/app1/users`)

### Manual Updates

You can also trigger manual updates that go to specific topics:

```bash
# Update only gateway
curl -X POST http://opal-server:7002/data/config \
  -H "Authorization: Bearer $DATASOURCE_TOKEN" \
  -d '{
    "entries": [{
      "url": "https://api.internal/app1/users",
      "dst_path": "/services/app1/users",
      "topics": ["gateway_data"]
    }],
    "reason": "Manual gateway refresh"
  }'

# Update both service sidecar and gateway
curl -X POST http://opal-server:7002/data/config \
  -H "Authorization: Bearer $DATASOURCE_TOKEN" \
  -d '{
    "entries": [{
      "url": "https://api.internal/app1/users",
      "dst_path": "/services/app1/users",
      "topics": ["app1:data:app1", "gateway_data"]
    }],
    "reason": "App1 users updated"
  }'
```

## OPA Data Structure

### At Gateway OPA

```json
{
  "shared": {
    "jwks": { /* JWKS keys */ }
  },
  "services": {
    "app1": {
      "users": [ /* app1 users */ ],
      "roles": [ /* app1 roles */ ]
    },
    "app2": {
      "users": [ /* app2 users */ ],
      "permissions": [ /* app2 permissions */ ]
    }
  }
}
```

### At App1 Sidecar OPA

```json
{
  "shared": {
    "jwks": { /* JWKS keys */ }
  },
  "services": {
    "app1": {
      "users": [ /* app1 users only */ ],
      "roles": [ /* app1 roles only */ ]
    }
  }
}
```

Note: Different `dst_path` values can be used in scope configs vs manual updates to create different data structures in gateway vs sidecars.

## Benefits

### ✅ Clean Separation
- Service mesh clients only see their own data (scope-specific topics)
- Gateway sees all data (shared topic)

### ✅ No Wildcard Needed
- Gateway subscribes to single topic: `gateway_data`
- No need for pattern matching or wildcards

### ✅ Gateway Config Never Changes
- Add new scopes (app3, app4, etc.) without touching gateway configuration
- Gateway automatically receives updates from all scopes

### ✅ Flexible Updates
- Can update gateway only, sidecar only, or both
- Control granularity per update

### ✅ Maintains Scope Isolation
- Service mesh clients remain fully isolated
- Gateway aggregation doesn't affect sidecar behavior

## Adding New Services

When adding a new service (app3), just configure the scope with dual topics:

```bash
curl -X PUT http://opal-server:7002/scopes/app3 \
  -d '{
    "scope_id": "app3",
    "policy": {
      "source_type": "git",
      "url": "https://github.com/yourorg/policies",
      "branch": "main",
      "directories": ["."],
      "auth": {"auth_type": "none"}
    },
    "data": {
      "entries": [
        {
          "url": "https://api.internal/app3/users",
          "dst_path": "/services/app3/users",
          "topics": ["app3:data:app3", "gateway_data"],  # Dual topics
          "periodic_update_interval": 60
        }
      ]
    }
  }'
```

Then deploy the app3 client with policy filtering:
```yaml
app3-sidecar:
  environment:
    - OPAL_SCOPE_ID=app3
    - OPAL_POLICY_SUBSCRIPTION_DIRS=.:services/app3  # Client filters policies
```

**No gateway reconfiguration needed!** The gateway automatically starts receiving app3 updates via the `gateway_data` topic.

## Best Practices

1. **Consistent Topic Naming:**
   - Scope-specific: `{scope_id}:data:{scope_id}`
   - Shared: `gateway_data`

2. **Consistent Data Paths:**
   - Gateway: `/services/{service_id}/*`
   - Sidecars: `/{service_id}/*` or `/services/{service_id}/*`

3. **JWKS Sharing:**
   - Configure JWKS in each scope with `gateway_data` topic
   - Gateway and all sidecars get the same JWKS data
   - Mounted at `/shared/jwks` in all clients

4. **Polling Intervals:**
   - Use appropriate intervals per data source (JWKS: 3600s, users: 60s)
   - Same intervals apply to both topics

5. **Testing:**
   ```bash
   # Verify gateway receives all service data
   curl http://gateway-opa:8181/v1/data/services
   
   # Verify app1 sidecar only sees app1 data
   curl http://app1-sidecar:8181/v1/data/services/app1
   ```

## Limitations

- Requires configuring each scope with dual topics
- Cannot dynamically add gateway topic to existing scopes (need to update scope config)
- All scope data goes to gateway (no filtering at topic level)

## Alternative: Topic Per Data Type

Instead of one shared topic, you could use multiple shared topics:

```json
{
  "topics": ["app1:data:app1", "gateway_users", "gateway_roles"]
}
```

Then gateway subscribes to:
```yaml
- OPAL_DATA_TOPICS=gateway_users,gateway_roles,gateway_jwks
```

This allows more granular control over what data the gateway receives.
