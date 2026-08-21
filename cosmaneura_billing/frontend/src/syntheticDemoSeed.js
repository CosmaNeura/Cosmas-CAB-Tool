const DIGEST = /^[0-9a-f]{64}$/;

export function seedConfirmationIsSafe(result, descriptor) {
  const outbound = result?.outbound;
  return Boolean(
    descriptor
    && DIGEST.test(descriptor.seedDigest)
    && Number.isInteger(descriptor.caseCount)
    && descriptor.caseCount > 0
    && result?.seedDigest === descriptor.seedDigest
    && result?.casesReady === descriptor.caseCount
    && typeof result?.replayed === "boolean"
    && outbound?.schedulerEnabled === false
    && outbound?.patientMessagingEnabled === false
    && outbound?.operationallyActivated === false
    && outbound?.automaticContact === false
    && outbound?.transport === "record_only"
  );
}

export function syntheticSeedErrorMessage(error) {
  switch (error?.status) {
    case 401:
      return "Your session expired. Sign in again before loading synthetic tasks.";
    case 403:
      return "Admin access is required to load the synthetic demo tasks.";
    case 409:
      return "A safety latch or existing chase state blocked the synthetic demo setup.";
    case 422:
      return "The approved synthetic demo request was rejected. Refresh and try again.";
    default:
      if (error?.status >= 500) {
        return "Synthetic demo setup is temporarily unavailable. Try again later.";
      }
      return "Could not reach synthetic demo setup. Check the connection and try again.";
  }
}
