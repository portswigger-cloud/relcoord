# Image Version Service Interface

## Status

Draft MVP interface definition.

## Summary

This document defines the external interface of a small HTTP service that keeps track of known versions for container images and returns the highest known version for each image according to the [Semantic Versioning 2.0.0](https://semver.org/) ordering rules.

The purpose of this service is to remove the need to create configuration-only pull requests when deploying a newly built software version. Instead of checking image tag updates into the same git-managed configuration repository used by `manifest-builder`, `manifest-builder` will query this service at manifest generation time.

For the first iteration, the service has no concept of environments, promotion, deployment history, or test results. It only stores known versions for images and answers "what is the latest known version?".

## Goals

- Allow external systems to register that a specific version exists for a specific image.
- Return the highest known version for a given image using SemVer comparison rules.
- Return latest versions for multiple images in one request so `manifest-builder` can resolve all required tags efficiently.
- Keep the interface small and stable.
- Preserve a clean path toward future policy-based selection, such as promotion based on deployment and test outcomes.

## Non-goals

- Environment-specific version selection.
- Automated promotion or rollout policy.
- Tracking deployments, test executions, or release eligibility.
- Support for non-SemVer ordering in the MVP.
- Image discovery from container registries in the MVP.

## Context

`manifest-builder` already generates Kubernetes manifests from configuration files stored in git. This is convenient for configuration changes, but it creates friction for repetitive image tag bumps. Those updates do not meaningfully change the desired deployment shape; they only identify which built artifact should be used.

This interface splits those responsibilities:

- `manifest-builder` remains responsible for deployment structure and configuration.
- The new service becomes responsible for image version knowledge.

## Service Responsibilities

The service has two required responsibilities:

1. Accept that a specific version of a specific image exists.
2. Return the highest known version for one or more images using SemVer precedence rules.

## API

All endpoints are versioned under `/v1`.

Image names may contain `/`, `:`, `.`, and other characters that are awkward in path parameters, so image identifiers are passed in JSON bodies.

### Register a known image version

`POST /v1/image-versions`

Request:

```json
{
  "image": "registry.example.com/payments/api",
  "version": "1.4.2"
}
```

Response when the version was newly recorded:

```json
{
  "image": "registry.example.com/payments/api",
  "version": "1.4.2",
  "created": true
}
```

Response when the version was already known:

```json
{
  "image": "registry.example.com/payments/api",
  "version": "1.4.2",
  "created": false
}
```

Behavior:

- The operation is idempotent.
- Re-registering the same `(image, version)` pair is successful and returns `created: false`.
- The service must reject invalid requests as described in the validation section.

### Get the latest known versions for images

`POST /v1/images/latest`

Request:

```json
{
  "images": [
    "registry.example.com/payments/api",
    "registry.example.com/payments/worker"
  ]
}
```

Response:

```json
{
  "versions": {
    "registry.example.com/payments/api": "1.4.2",
    "registry.example.com/payments/worker": "2.1.0"
  }
}
```

If an image has no known versions, the response remains successful and includes `null` for that image:

```json
{
  "versions": {
    "registry.example.com/payments/api": null
  }
}
```

### Health endpoint

`GET /healthz`

Response:

```json
{
  "status": "ok"
}
```

## Validation Rules

### Image field

- Required.
- Must be a non-empty string.
- Treated as an opaque identifier.

### Version field

- Required.
- Must be a non-empty string.
- Must be valid Semantic Versioning 2.0.0.

### Images list

- Required for `POST /v1/images/latest`.
- Must be a JSON array of non-empty strings.
- Duplicate image names may be accepted, but the response must contain each image at most once.

## Version Ordering Rules

Version ordering must follow [semver.org](https://semver.org/) exactly:

- Higher `major`, then `minor`, then `patch` wins.
- Pre-release versions sort lower than the associated normal release.
- Build metadata does not affect precedence.

Examples:

- `1.2.0` > `1.1.99`
- `1.2.0` > `1.2.0-rc.1`
- `1.2.0+build5` has the same precedence as `1.2.0+build7`

Because build metadata does not affect precedence, equal-precedence version strings need explicit service behavior. For the MVP, the service should reject registration of a second version string for the same image if it differs only by build metadata from an already-known version with equal precedence.

## Response Semantics

### Registration responses

- If the image/version pair was not previously known, the service returns success with `created: true`.
- If the image/version pair was already known, the service returns success with `created: false`.

### Latest-version responses

- If an image has one or more known versions, the response contains the highest-precedence version string for that image.
- If an image has no known versions, the response contains `null` for that image.

## Error Handling

Recommended response behavior:

- `200 OK` for successful reads.
- `200 OK` for idempotent duplicate registration with `created: false`.
- `201 Created` for first-time registration if the implementation wants to distinguish creation.
- `400 Bad Request` for malformed JSON, missing fields, or invalid SemVer.
- `500 Internal Server Error` for unexpected failures.

Example invalid version response:

```json
{
  "error": "invalid_version",
  "message": "version must be valid Semantic Versioning 2.0.0"
}
```

## Integration Contract with `manifest-builder`

`manifest-builder` remains the component that knows which images are relevant to a manifest. The version service does not need to understand Kubernetes resources or application topology.

Recommended interaction:

1. `manifest-builder` determines which images require version resolution.
2. It calls `POST /v1/images/latest` with that image list.
3. It receives a map from image name to version or `null`.
4. It injects those versions into the manifest generation process.
5. If any required image resolves to `null`, `manifest-builder` decides whether to fail or apply fallback behavior.

## Future Evolution

This interface intentionally leaves room for a smarter release selection system.

Future additions could include:

- environment-aware desired versions
- deployment history per environment
- automated test result ingestion
- promotion rules such as "eligible for staging after successful deployment in dev"
- policy-based selection that returns the best eligible version rather than simply the highest known version

The MVP should keep this interface stable unless those future capabilities require additional endpoints.

## Open Questions

- Should `manifest-builder` fail hard when an image has no known version, or should it allow local fallback rules?
- Which component is responsible for calling the registration endpoint: CI, a release job, or some registry-watching process?
- Is there any existing image tagging behavior that is not strict SemVer and would need to be normalized before using this service?
