/** Oldest-polled first; a long round must not revisit its first 25 markets forever. */
export function dueTradeMarkets(activity, polledAt, { now = Date.now(), every = 120000,
  limit = 25 } = {}) {
  return [...activity.keys()]
    .filter(id => now - (polledAt.get(id) ?? -Infinity) >= every)
    .sort((a,b) => {
      const pa = polledAt.get(a) ?? -Infinity, pb = polledAt.get(b) ?? -Infinity;
      return (pa === pb ? 0 : pa < pb ? -1 : 1)
        || (activity.get(b) ?? 0) - (activity.get(a) ?? 0) || a.localeCompare(b);
    }).slice(0, Math.max(0, limit));
}
