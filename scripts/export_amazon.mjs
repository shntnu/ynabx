/**
 * Scrape authenticated Amazon payment and order pages through a browser tab.
 *
 * The tab may be a Codex Chrome tab or a Playwright Page. This module never
 * reads cookies or browser storage. It uses the authenticated page normally.
 */

import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rename, writeFile } from "node:fs/promises";
import path from "node:path";

const AMAZON_ORIGIN = "https://www.amazon.com";
const PAYMENT_URL = `${AMAZON_ORIGIN}/cpe/yourpayments/transactions`;
const NEXT_PAYMENT_SELECTOR = 'input[type="submit"][name*="DefaultNextPageNavigationEvent"]';

function playwrightSurface(tab) {
  return tab.playwright ?? tab;
}

async function evaluateOnce(tab, callback) {
  return playwrightSurface(tab).evaluate(callback);
}

async function evaluate(tab, callback, retries = 3) {
  let lastError = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      return await evaluateOnce(tab, callback);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw lastError;
}

async function click(tab, selector) {
  return playwrightSurface(tab).locator(selector).click();
}

async function navigate(tab, url, retries = 5) {
  let lastError = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      await tab.goto(url);
      return;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw lastError;
}

function paymentPageFingerprint(rows) {
  return JSON.stringify(
    rows.map((row) => [row.transaction_date, row.amount_text, row.order_id, row.payment_instrument, row.merchant]),
  );
}

async function waitForPaymentTransition(tab, previousFingerprint = null, attempts = 40) {
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const page = await evaluate(tab, extractPayments);
    if (page.rows.length) {
      const fingerprint = paymentPageFingerprint(page.rows);
      if (previousFingerprint === null || fingerprint !== previousFingerprint) {
        return { page, terminal: false };
      }
      if (!page.hasNext) return { page, terminal: true };
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return null;
}

async function advancePaymentPage(tab, previousRows, retries = 3) {
  const previousFingerprint = paymentPageFingerprint(previousRows);
  let lastError = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      await click(tab, NEXT_PAYMENT_SELECTOR);
    } catch (error) {
      lastError = error;
    }
    let transition = await waitForPaymentTransition(tab, previousFingerprint);
    if (transition) return transition;
    try {
      await evaluateOnce(tab, () => {
        const next = document.querySelector('input[type="submit"][name*="DefaultNextPageNavigationEvent"]');
        if (!next || next.disabled) throw new Error("Amazon next payment button is unavailable");
        next.click();
      });
    } catch (error) {
      lastError = error;
    }
    transition = await waitForPaymentTransition(tab, previousFingerprint);
    if (transition) return transition;
  }
  if (previousRows.length < 20) return { page: { rows: previousRows, hasNext: false }, terminal: true };
  throw new Error(`Amazon payment page did not advance after ${retries} attempts`, { cause: lastError });
}

async function progress(callback, event) {
  if (callback) await callback(event);
}

function orderUrl(year, pageNumber, orderFilter) {
  const query = new URLSearchParams({
    timeFilter: `year-${year}`,
    page: String(pageNumber),
  });
  if (orderFilter) query.set("orderFilter", orderFilter);
  return `${AMAZON_ORIGIN}/your-orders/orders?${query}`;
}

function extractOrderMeta() {
  const content = document.querySelector(".your-orders-content-container__content.js-yo-main-content");
  const text = content?.innerText ?? "";
  const countMatch = text.match(/([\d,]+) orders? placed/i);
  const pages = Array.from(document.querySelectorAll(".a-pagination a"))
    .map((anchor) => Number(new URL(anchor.href).searchParams.get("page")))
    .filter(Number.isFinite);
  return {
    cardCount: document.querySelectorAll("div.order-card.js-order-card").length,
    orderCount: countMatch ? Number(countMatch[1].replaceAll(",", "")) : null,
    maxPage: pages.length ? Math.max(...pages) : 0,
    title: document.title,
  };
}

function extractOrders() {
  const absoluteUrl = (href) => (href ? new URL(href, location.origin).href : null);
  return Array.from(document.querySelectorAll("div.order-card.js-order-card")).map((card) => {
    const headerItems = Array.from(card.querySelectorAll(".order-header__header-list-item")).map((element) =>
      element.innerText.trim(),
    );
    const dateLines = (headerItems[0] ?? "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const totalText = (headerItems.find((text) => /^TOTAL\b/i.test(text)) ?? "")
      .replace(/^TOTAL\s*/i, "")
      .trim();
    const orderText = headerItems.find((text) => /^ORDER\s*#/i.test(text)) ?? "";
    const orderId = orderText.match(/(?:\d{3}-\d{7}-\d{7}|D\d{2}-\d{7}-\d{7})/)?.[0] ?? null;
    const links = Array.from(card.querySelectorAll("a"));
    const details = links.find((anchor) => /View order details/i.test(anchor.innerText ?? ""));
    const invoice = links.find((anchor) => /View invoice/i.test(anchor.innerText ?? ""));
    const itemsByKey = new Map();
    for (const titleElement of card.querySelectorAll(".yohtmlc-product-title")) {
      const title = titleElement.innerText?.trim() ?? null;
      const productAnchor = titleElement.closest('a[href*="/dp/"]') ?? titleElement.querySelector('a[href*="/dp/"]');
      const productUrl = productAnchor?.href ?? null;
      const asin = productUrl?.match(/\/dp\/([A-Z0-9]{10})/i)?.[1]?.toUpperCase() ?? null;
      if (title && !itemsByKey.has(asin ?? title)) {
        itemsByKey.set(asin ?? title, { title, asin, product_url: productUrl });
      }
    }
    return {
      order_id: orderId,
      order_date: dateLines.at(-1) ?? null,
      order_total_text: totalText || null,
      order_total: totalText ? Number(totalText.replace(/[^0-9.-]/g, "")) : null,
      statuses: [
        ...new Set(
          Array.from(card.querySelectorAll(".yohtmlc-shipment-status-primaryText"))
            .map((element) => element.innerText.trim())
            .filter(Boolean),
        ),
      ],
      items: [...itemsByKey.values()],
      order_details_url: absoluteUrl(details?.getAttribute("href")),
      invoice_url: absoluteUrl(invoice?.getAttribute("href")),
      source_page: location.href,
    };
  });
}

function extractPayments() {
  const rows = [];
  const roots = Array.from(document.querySelectorAll(".a-box-inner.a-padding-none")).filter((element) =>
    element.querySelector(".apx-transaction-date-container"),
  );
  for (const root of roots) {
    let transactionDate = null;
    for (const child of root.children) {
      if (child.matches(".apx-transaction-date-container")) {
        transactionDate = (child.innerText ?? "").trim();
        continue;
      }
      for (const item of child.querySelectorAll(".apx-transactions-line-item-component-container")) {
        const lines = (item.innerText ?? "")
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
        const amountText = lines.find((line) => /^[+-]?\$[\d,]+\.\d{2}$/.test(line)) ?? null;
        const orderLabel = lines.find((line) => /(?:Refund:\s*)?Order #/.test(line)) ?? null;
        const orderId = orderLabel?.match(/(?:\d{3}-\d{7}-\d{7}|D\d{2}-\d{7}-\d{7})/)?.[0] ?? null;
        const orderAnchor = Array.from(item.querySelectorAll("a")).find((anchor) => /orderID=/.test(anchor.href ?? ""));
        const amount = amountText ? Number(amountText.replace(/[$,]/g, "")) : null;
        rows.push({
          transaction_date: transactionDate,
          payment_instrument: lines[0] ?? null,
          amount_text: amountText,
          amount,
          status: lines.includes("Pending") ? "Pending" : "Completed",
          kind: /^Refund:/i.test(orderLabel ?? "") || (amount !== null && amount > 0) ? "refund_or_credit" : "charge",
          order_id: orderId,
          order_label: orderLabel,
          merchant: lines.at(-1) ?? null,
          order_url: orderAnchor?.href ?? null,
        });
      }
    }
  }
  const next = document.querySelector('input[type="submit"][name*="DefaultNextPageNavigationEvent"]');
  return { rows, hasNext: Boolean(next && !next.disabled) };
}

async function loadOrderMeta(tab, url, retries) {
  let meta = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    await navigate(tab, url);
    meta = await evaluate(tab, extractOrderMeta);
    if (meta.orderCount !== null || meta.cardCount > 0) return meta;
  }
  throw new Error(`Amazon returned no order metadata after ${retries} attempts: ${url}`);
}

async function loadOrderPage(tab, url, retries, expectedRows, seenOrderIds) {
  let lastProblem = "no order cards";
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    await navigate(tab, url);
    for (let renderAttempt = 1; renderAttempt <= 40; renderAttempt += 1) {
      const rows = await evaluate(tab, extractOrders);
      const orderIds = rows.map((row) => row.order_id);
      const duplicateIds = orderIds.filter((orderId) => orderId && seenOrderIds.has(orderId));
      if (rows.length === expectedRows && orderIds.every(Boolean) && !duplicateIds.length) return rows;
      lastProblem = `expected ${expectedRows} new rows, received ${rows.length}, duplicate IDs ${duplicateIds.length}`;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw new Error(`Amazon order page failed validation after ${retries} attempts (${lastProblem}): ${url}`);
}

export async function scrapePayments(tab, options = {}) {
  const { maxPages = 1_000, allowPartial = false, onProgress = null } = options;
  await navigate(tab, PAYMENT_URL);
  const payments = [];
  const firstTransition = await waitForPaymentTransition(tab);
  if (!firstTransition) throw new Error("Amazon returned no payment rows on the first page");
  let page = firstTransition.page;
  for (let pageNumber = 0; pageNumber < maxPages; pageNumber += 1) {
    payments.push(
      ...page.rows.map((row, rowIndex) => ({
        ...row,
        source_page_index: pageNumber,
        row_index: rowIndex,
      })),
    );
    await progress(onProgress, { phase: "payments", page: pageNumber + 1, rows: payments.length });
    if (!page.hasNext) return payments;
    const transition = await advancePaymentPage(tab, page.rows);
    if (transition.terminal) return payments;
    page = transition.page;
  }
  if (allowPartial) return payments;
  throw new Error(`Amazon payment history exceeded the maxPages limit of ${maxPages}`);
}

export async function scrapeOrders(tab, options = {}) {
  const currentYear = new Date().getFullYear();
  const {
    years = Array.from({ length: currentYear - 2020 + 1 }, (_, index) => currentYear - index),
    orderFilters = [null, "digital"],
    retries = 3,
    maxPagesPerYear = Number.POSITIVE_INFINITY,
    onProgress = null,
  } = options;
  const ordersById = new Map();
  for (const orderFilter of orderFilters) {
    const orderType = orderFilter ?? "regular";
    for (const year of years) {
      const yearOrderIds = new Set();
      const firstUrl = orderUrl(year, 0, orderFilter);
      const meta = await loadOrderMeta(tab, firstUrl, retries);
      if (meta.orderCount === 0) continue;
      const pageCount = Math.min(meta.maxPage + 1, maxPagesPerYear);
      for (let pageNumber = 0; pageNumber < pageCount; pageNumber += 1) {
        const expectedRows =
          meta.orderCount === null
            ? pageNumber < meta.maxPage
              ? 10
              : meta.cardCount
            : Math.min(10, Math.max(meta.orderCount - pageNumber * 10, 0));
        const rows = await loadOrderPage(
          tab,
          orderUrl(year, pageNumber, orderFilter),
          retries,
          expectedRows,
          yearOrderIds,
        );
        for (const row of rows) {
          if (!row.order_id) throw new Error(`Amazon returned an order without an ID: ${row.source_page}`);
          ordersById.set(row.order_id, row);
          yearOrderIds.add(row.order_id);
        }
        await progress(onProgress, {
          phase: "orders",
          orderType,
          year,
          page: pageNumber + 1,
          pages: pageCount,
          rows: ordersById.size,
        });
      }
      if (Number.isFinite(maxPagesPerYear)) continue;
      if (meta.orderCount !== null && yearOrderIds.size !== meta.orderCount) {
        throw new Error(
          `Amazon reported ${meta.orderCount} ${orderType} orders for ${year}, but only ${yearOrderIds.size} were collected`,
        );
      }
    }
  }
  return [...ordersById.values()];
}

export async function scrapeAmazon(tab, options = {}) {
  const { onProgress = null, paymentOptions = {}, orderOptions = {} } = options;
  const payments = await scrapePayments(tab, { ...paymentOptions, onProgress });
  const orders = await scrapeOrders(tab, { ...orderOptions, onProgress });
  return { payments, orders };
}

export function makeSnapshotId(now = new Date()) {
  return now.toISOString().replaceAll("-", "").replaceAll(":", "").replace(/\.\d{3}Z$/, "Z");
}

function sha256(text) {
  return createHash("sha256").update(text).digest("hex");
}

export async function writeAmazonSnapshot(tab, options = {}) {
  const {
    outputRoot = path.resolve("data/external/raw/amazon/snapshots"),
    snapshotId = makeSnapshotId(),
    onProgress = null,
    paymentOptions = {},
    orderOptions = {},
  } = options;
  const data = await scrapeAmazon(tab, { onProgress, paymentOptions, orderOptions });
  return writeAmazonDataSnapshot(data, { outputRoot, snapshotId, onProgress });
}

export async function writeAmazonDataSnapshot(data, options = {}) {
  const {
    outputRoot = path.resolve("data/external/raw/amazon/snapshots"),
    snapshotId = makeSnapshotId(),
    onProgress = null,
  } = options;
  const { payments, orders } = data;
  if (!Array.isArray(payments) || !payments.length) throw new Error("payments must be a nonempty array");
  if (!Array.isArray(orders) || !orders.length) throw new Error("orders must be a nonempty array");
  if (orders.some((order) => !order.order_id)) throw new Error("every order must have an order_id");
  const root = path.resolve(outputRoot);
  await mkdir(root, { recursive: true });
  const buildingDirectory = await mkdtemp(path.join(root, ".building-"));
  const paymentsName = `amazon_payment_transactions_${snapshotId}.json`;
  const ordersName = `amazon_orders_${snapshotId}.json`;
  const paymentsJson = `${JSON.stringify(payments, null, 2)}\n`;
  const ordersJson = `${JSON.stringify(orders, null, 2)}\n`;
  const manifest = {
    schema_version: 1,
    snapshot_id: snapshotId,
    captured_at: new Date().toISOString(),
    source: "Authenticated Amazon Your Payments and Your Orders pages",
    counts: { payments: payments.length, orders: orders.length },
    files: {
      payments: { path: paymentsName, sha256: sha256(paymentsJson) },
      orders: { path: ordersName, sha256: sha256(ordersJson) },
    },
  };
  await writeFile(path.join(buildingDirectory, paymentsName), paymentsJson, "utf8");
  await writeFile(path.join(buildingDirectory, ordersName), ordersJson, "utf8");
  await writeFile(path.join(buildingDirectory, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  const finalDirectory = path.join(root, snapshotId);
  await rename(buildingDirectory, finalDirectory);
  await progress(onProgress, { phase: "saved", directory: finalDirectory, payments: payments.length, orders: orders.length });
  return { directory: finalDirectory, manifest };
}
