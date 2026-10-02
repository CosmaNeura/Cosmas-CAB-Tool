import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";

test("actual E/M data card uses validated summaries and preserves absent-ledger fallback", (t) => {
  const file = fileURLToPath(new URL("./BillingReview.jsx", import.meta.url));
  const directory = mkdtempSync(join(tmpdir(), "cab-data-card-"));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  const bundle = join(directory, "render.cjs");
  // Export the existing private component from a temporary build, without editing source.
  const source = readFileSync(file, "utf8") + `
import {renderToStaticMarkup} from "react-dom/server";
export const renderDataCard = (props) => renderToStaticMarkup(<EmReview {...props} />);
`;
  buildSync({stdin: {contents: source, resolveDir: dirname(file), sourcefile: file, loader: "jsx"},
    outfile: bundle, bundle: true, platform: "node", format: "cjs",
    nodePaths: [resolve(dirname(file), "../node_modules")], logLevel: "silent"});
  const {renderDataCard} = createRequire(import.meta.url)(bundle);
  const golden = JSON.parse(readFileSync(new URL("../../tests/fixtures/mdm_data_review.json", import.meta.url)));
  for (const mode of ["complete", "incomplete", "invalid", "absent"]) {
    let ledger = {...structuredClone(golden.display), level: "moderate"};
    if (mode === "incomplete") {
      Object.assign(ledger.events[0], {status: "held", evidence_spans: [], evidence_complete: false});
      ledger.evidence_complete = false;
    }
    if (mode === "invalid") ledger.events[0].evidence_spans[0].text = "Not in source";
    if (mode === "absent") ledger = undefined;
    const em = {status: "held", suggested_level: "99213", data_review: ledger,
      mdm_facts: [{element: "data", finding: "moderate", evidence_span: null}], evidence_spans: []};
    const html = renderDataCard({em, supportedEm: null, encounter: golden.encounter, dataReview: ledger});
    const heading = html.match(/text-slate-500">data<\/p><p[^>]*>([^<]*)<\/p>/)?.[1];
    assert.ok(heading, mode);
    if (mode === "complete") assert.equal(heading, "Element level: moderate.");
    else if (mode === "absent") assert.equal(heading, "moderate");
    else {
      assert.doesNotMatch(heading, /moderate/);
      assert.match(heading, mode === "invalid" ? /Data evidence not verified/ : /Summary unavailable/);
    }
  }
});
