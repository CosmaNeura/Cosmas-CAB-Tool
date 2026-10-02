import React from "react";
import { sourceVisitContextDisplay } from "./sourceVisitContext.js";

export default function SourceVisitContext({value, expectedSource}) {
  const display = sourceVisitContextDisplay(value, expectedSource);
  return <div aria-label="Source visit context" className="mt-3 text-xs text-slate-600">
    <dl className="grid gap-2 sm:grid-cols-2">
      <div><dt className="font-medium">Date of service</dt><dd className="mt-1">{display.serviceDate}</dd></div>
      <div><dt className="font-medium">Note status</dt><dd className="mt-1">{display.noteStatus}</dd></div>
    </dl>
    {display.qualification && <p className="mt-2">{display.qualification}</p>}
  </div>;
}
