import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "../node_modules/typescript/lib/typescript.js";

// File URLs give native ESM a stable identity for cyclic imports, unlike nested data URLs.
export function createModuleLoader(baseUrl) {
  const directory = mkdtempSync(join(tmpdir(), "crypto-test-modules-"));
  const modules = new Map();
  process.once("exit", () => rmSync(directory, { recursive: true, force: true }));

  function load(sourceUrl, imports = {}) {
    if (modules.has(sourceUrl.href)) return modules.get(sourceUrl.href);
    const target = pathToFileURL(join(directory, `${modules.size}.mjs`));
    // Reserve the URL before traversing dependencies so back edges reuse this module.
    modules.set(sourceUrl.href, target.href);
    let { outputText } = ts.transpileModule(readFileSync(sourceUrl, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    });
    for (const { fileName: specifier } of ts.preProcessFile(outputText).importedFiles) {
      const resolved = imports[specifier] ?? (specifier.startsWith(".")
        ? load(new URL(`${specifier}.ts`, sourceUrl))
        : import.meta.resolve(specifier));
      outputText = outputText.replaceAll(JSON.stringify(specifier), JSON.stringify(resolved));
    }
    writeFileSync(target, outputText);
    return target.href;
  }

  return (path, imports) => load(new URL(path, baseUrl), imports);
}
