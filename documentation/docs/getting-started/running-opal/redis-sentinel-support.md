---
title: Redis Sentinel Support in OPAL Server
---

# Redis Sentinel Support in OPAL Server

OPAL Server now supports Redis Sentinel for high availability and automatic failover. This allows OPAL to use a Redis cluster managed by Sentinel for configuration storage, ensuring reliability and resilience.

## Configuration

### Sentinel Connection String

Set the configuration storage URI using the following format:

```
redis+sentinel://host1:26379,host2:26380,host3:26381/mymaster?password=yourpassword&ssl=true
```

- **hostN:port**: Sentinel node addresses
- **/mymaster**: Sentinel service name (default: `mymaster`)
- **password**: Redis authentication (optional)
- **ssl**: Enable SSL/TLS (optional, `true` or `false`)

### Example (docker-compose)

```yaml
environment:
  - OPAL_STORE_URI=redis+sentinel://127.0.0.1:26379,127.0.0.1:26380,127.0.0.1:26381/mymaster
```

### SSL/TLS Options

- `ssl=true`: Enable SSL/TLS
- `ssl_cert_reqs=required|optional|none`: Certificate verification mode
- `ssl_ca_certs=/path/to/ca.crt`: CA certificate file

## Failover and High Availability

- OPAL automatically reconnects to the new master if a failover occurs.
- Sentinel monitors Redis nodes and promotes a replica if the master fails.

## Environment Variables

- `OPAL_STORE_URI`: Sentinel connection string for config storage

## Troubleshooting

- Ensure all Sentinel nodes are reachable from OPAL Server.
- Use the correct service name (`mymaster` by default).
- For SSL, provide valid certificates and set verification options as needed.

## References

- [Redis Sentinel Documentation](https://redis.io/docs/management/sentinel/)
- [OPAL Configuration Guide](../)
