// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Descriptive public names; the existing v1 grant and result contract is retained.
export * from '../gate/content-consent.mjs';
export {
  createContentConsent as createFileAccessPolicy,
  CONTENT_CONSENT_LIMITS as FILE_ACCESS_LIMITS,
} from '../gate/content-consent.mjs';
