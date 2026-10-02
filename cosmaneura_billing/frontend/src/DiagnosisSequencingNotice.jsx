import React from "react";
import { sequencingMessage, validDiagnosisSequencing } from "./diagnosisSequencing.js";

export default function DiagnosisSequencingNotice({ value }) {
  if (validDiagnosisSequencing(value) && !value.requires_review) return null;
  return <p role="status" className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
    {sequencingMessage(value)}
  </p>;
}
