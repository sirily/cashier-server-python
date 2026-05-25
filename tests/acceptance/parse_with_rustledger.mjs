import http from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { initSync, Ledger } from "@rustledger/wasm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const wasmBytes = readFileSync(
  path.join(__dirname, "node_modules/@rustledger/wasm/rustledger_wasm_bg.wasm")
);
initSync({ module: wasmBytes });

const PORT = process.env.PORT;
if (!PORT) {
  console.error("PORT environment variable is required");
  process.exit(1);
}

const url = `http://127.0.0.1:${PORT}/infrastructure?file_path=main.bean`;

http.get(url, async (res) => {
  let body = "";
  res.on("data", (chunk) => (body += chunk));
  res.on("end", async () => {
    try {
      const data = JSON.parse(body);
      const content = data.content;
      const ledger = Ledger.fromFiles({ "main.bean": content }, "main.bean");
      const errors = ledger.getErrors();
      if (errors.length !== 0) {
        console.error(`RustLedger parse errors (${errors.length}):`);
        for (const e of errors) {
          console.error(`  - ${e}`);
        }
        process.exit(1);
      }
      console.log(`OK: RustLedger parsed materialized snapshot with 0 errors`);
    } catch (err) {
      console.error("Failed to parse with RustLedger:", err.message);
      process.exit(1);
    }
  });
}).on("error", (err) => {
  console.error("HTTP request failed:", err.message);
  process.exit(1);
});
