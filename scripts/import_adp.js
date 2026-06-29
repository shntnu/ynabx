(async () => {
  const getJSON = async (url) => {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${response.status} ${url}`);
    return response.json();
  };

  const version = await getJSON("/myadp_prefix/myadpapi/core/v1/version");
  const list = await getJSON(
    `/myadp_prefix/payroll/v1/workers/${version.associateoid}/pay-statements` +
      "?adjustments=yes&numberoflastpaydates=300",
  );
  const statements = [];
  for (const item of list.payStatements) {
    const detail = await getJSON(`/myadp_prefix${item.payDetailUri.href}?rolecode=employee`);
    statements.push({
      ...detail.payStatement,
      _adp: { statementID: item.payDetailUri.href },
    });
  }
  if (statements.length !== list.payStatements.length) throw new Error("incomplete ADP export");

  const payload = {
    exportedAt: new Date().toISOString(),
    source: "MyADP employee pay-statements API",
    statements,
  };
  const date = new Date().toISOString().slice(0, 10);
  const filename = `adp-pay-statements-${date}.json`;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([JSON.stringify(payload)], { type: "application/json" }));
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
  return { filename, statements: statements.length };
})();
