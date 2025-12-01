package authz.jwt

import rego.v1

# Default deny
default allow := false

# JWKS data from well-known endpoint (loaded by OPAL at /shared/jwks)
jwks := data.shared.jwks

# Extract Bearer token from Authorization header
bearer_token := token if {
	authorization := input.headers.authorization
	startswith(authorization, "Bearer ")
	token := substring(authorization, 7, -1)
}

# Verify JWT and extract payload
verified_payload := payload if {
	token := bearer_token
	[valid, _, payload] := io.jwt.decode_verify(token, {
		"cert": jwks,
		"aud": input.expected_audience,  # Pass expected audience in input
		"iss": input.expected_issuer      # Pass expected issuer in input
	})
	valid == true
}

# Main authorization rule
allow if {
	payload := verified_payload
	has_required_scope(payload)
	not token_expired(payload)
}

# Check if token has required scope
has_required_scope(payload) if {
	required_scope := input.required_scope
	some scope in payload.scope
	scope == required_scope
}

# Alternative: Check if token has ANY of the required scopes
has_any_scope(payload) if {
	required_scopes := input.required_scopes
	some required in required_scopes
	some scope in payload.scope
	scope == required
}

# Check token expiration (additional safety check)
token_expired(payload) if {
	now := time.now_ns() / 1000000000  # Convert to seconds
	payload.exp < now
}

# Helper: Get specific JWK by kid (key ID)
get_jwk_by_kid(kid) := jwk if {
	some jwk in jwks.keys
	jwk.kid == kid
}

# Helper: Verify token was signed by expected key
verify_key_id(expected_kid) if {
	token := bearer_token
	[header, _, _] := io.jwt.decode(token)
	header.kid == expected_kid
}

# Example: Role-based authorization
allow_by_role if {
	payload := verified_payload
	required_role := input.required_role
	some role in payload.roles
	role == required_role
}

# Example: Permission-based authorization
allow_by_permission if {
	payload := verified_payload
	required_permission := input.required_permission
	some permission in payload.permissions
	permission == required_permission
}

# Example: Check user ID matches
allow_by_user if {
	payload := verified_payload
	payload.sub == input.user_id
}

# Debugging helpers
debug_token_info := info if {
	token := bearer_token
	[header, payload, _] := io.jwt.decode(token)
	info := {
		"header": header,
		"payload": payload,
		"kid": header.kid,
		"alg": header.alg,
		"exp": payload.exp,
		"iss": payload.iss,
		"aud": payload.aud
	}
}

debug_available_keys := keys if {
	keys := [key |
		some jwk in jwks.keys
		key := {
			"kid": jwk.kid,
			"alg": jwk.alg,
			"use": jwk.use,
			"kty": jwk.kty
		}
	]
}
