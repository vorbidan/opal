# JWKS Configuration for Well-Known Endpoints

## Configuration Applied

The OPAL server has been configured to fetch JWKS from a well-known endpoint and load it into OPA at `/v1/data/shared/jwks`.

### Data Source Configuration:
```json
{
  "url": "https://your-auth-server.com/.well-known/jwks.json",
  "topics": ["policy_data"],
  "dst_path": "/shared/jwks",
  "periodic_update_interval": 300
}
```

## Common Well-Known JWKS Endpoints

Replace `https://your-auth-server.com/.well-known/jwks.json` with your actual JWKS endpoint:

### Auth0:
```
https://YOUR-DOMAIN.auth0.com/.well-known/jwks.json
```

### Keycloak:
```
https://YOUR-KEYCLOAK-SERVER/realms/YOUR-REALM/protocol/openid-connect/certs
```

### Okta:
```
https://YOUR-DOMAIN.okta.com/oauth2/default/v1/keys
```

### Azure AD / Microsoft Entra ID:
```
https://login.microsoftonline.com/YOUR-TENANT-ID/discovery/v2.0/keys
```

### Google:
```
https://www.googleapis.com/oauth2/v3/certs
```

### AWS Cognito:
```
https://cognito-idp.REGION.amazonaws.com/USER-POOL-ID/.well-known/jwks.json
```

## Update docker-compose.yml

Edit line 93 in `docker-compose.yml` to use your actual JWKS endpoint:

```yaml
- OPAL_DATA_CONFIG_SOURCES={"config":{"entries":[
    {"url":"file:///data/authz.json","topics":["policy_data"],"dst_path":"/rules","periodic_update_interval":60},
    {"url":"https://YOUR-AUTH-SERVER/.well-known/jwks.json","topics":["policy_data"],"dst_path":"/shared/jwks","periodic_update_interval":300}
  ]}}
```

## Verify JWKS is Loaded

After starting the services:

```bash
# Start services
docker compose -f examples/cnap-mvp/docker-compose.yml up -d

# Wait for initialization
sleep 15

# Query JWKS from OPA
curl http://localhost:8181/v1/data/shared/jwks

# Expected output:
# {
#   "result": {
#     "keys": [
#       {
#         "kty": "RSA",
#         "use": "sig",
#         "kid": "...",
#         "alg": "RS256",
#         "n": "...",
#         "e": "AQAB"
#       }
#     ]
#   }
# }
```

## Access JWKS in Rego Policies

```rego
package authz

import rego.v1

# Access JWKS data
jwks := data.shared.jwks

# Verify JWT token using JWKS
allow if {
    # Extract token from Authorization header
    token := trim_prefix(input.headers.authorization, "Bearer ")
    
    # Verify JWT signature using JWKS from well-known endpoint
    io.jwt.decode_verify(token, {
        "cert": jwks_to_pem(data.shared.jwks),
        "aud": "your-audience",
        "iss": "https://your-auth-server.com"
    })
    
    # Additional authorization checks...
}

# Find key by kid (key ID)
get_jwk(kid) := jwk if {
    some jwk in data.shared.jwks.keys
    jwk.kid == kid
}

# Verify token with specific algorithm
verify_rs256(token) if {
    [header, payload, signature] := io.jwt.decode(token)
    jwk := get_jwk(header.kid)
    jwk.alg == "RS256"
    # Additional verification...
}
```

## Update Interval

The JWKS is fetched every **300 seconds (5 minutes)**. You can adjust this by changing `periodic_update_interval`:

- **60**: Every 1 minute (more frequent, higher load)
- **300**: Every 5 minutes (recommended)
- **900**: Every 15 minutes (less frequent)
- **3600**: Every 1 hour (for rarely changing keys)

## Troubleshooting

### Check OPAL Server Logs:
```bash
docker compose -f examples/cnap-mvp/docker-compose.yml logs -f opal-server
```

Look for:
```
HttpFetchProvider fetching from https://your-auth-server.com/.well-known/jwks.json
Publishing data update to topic: policy_data
```

### Check OPAL Client Logs:
```bash
docker compose -f examples/cnap-mvp/docker-compose.yml logs -f opal-client-smesh-app1
```

Look for:
```
Got data update: dst_path=/shared/jwks
Setting OPA data at /shared/jwks
```

### Common Issues:

1. **Network Access**: Ensure OPAL server can reach the JWKS endpoint
   - Check firewall rules
   - Verify DNS resolution
   - Test with: `docker compose exec opal-server curl https://your-auth-server.com/.well-known/jwks.json`

2. **SSL/TLS Issues**: If using self-signed certificates, you may need to configure trust
   - Add CA certificates to the container
   - Or use `OPAL_HTTP_NO_VERIFY=true` (not recommended for production)

3. **Rate Limiting**: Some providers rate-limit JWKS requests
   - Increase `periodic_update_interval` to reduce frequency
   - Cache JWKS locally if needed

## Alternative: Use File-Based JWKS

If you want to cache JWKS locally or for offline environments:

1. Download JWKS to a file:
   ```bash
   curl https://your-auth-server.com/.well-known/jwks.json > examples/cnap-mvp/jwks/jwks.json
   ```

2. Update configuration to use file:
   ```json
   {"url":"file:///jwks/jwks.json","topics":["policy_data"],"dst_path":"/shared/jwks","periodic_update_interval":300}
   ```

3. Mount the directory:
   ```yaml
   volumes:
     - ./jwks:/jwks:ro
   ```

## Example: Complete Configuration with Auth0

For Auth0 tenant `mycompany.auth0.com`:

```yaml
- OPAL_DATA_CONFIG_SOURCES={"config":{"entries":[
    {"url":"file:///data/authz.json","topics":["policy_data"],"dst_path":"/rules","periodic_update_interval":60},
    {"url":"https://mycompany.auth0.com/.well-known/jwks.json","topics":["policy_data"],"dst_path":"/shared/jwks","periodic_update_interval":300}
  ]}}
```

Rego policy:
```rego
package authz

import rego.v1

allow if {
    token := trim_prefix(input.headers.authorization, "Bearer ")
    io.jwt.decode_verify(token, {
        "cert": data.shared.jwks,
        "aud": "https://mycompany.com/api",
        "iss": "https://mycompany.auth0.com/"
    })
}
```
