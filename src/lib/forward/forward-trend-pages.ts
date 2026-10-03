/** Stable pagination advances by the returned count, including when the API cap is below 500. */
export async function readTrendPages<T>(
  page: (offset: number) => PromiseLike<{
    data: unknown[] | null;
    error: { message: string } | null;
  }>,
): Promise<T[]> {
  const rows: T[] = [];
  for (;;) {
    const result = await page(rows.length);
    if (result.error) throw new Error(`Forward trend paged read failed: ${result.error.message}`);
    if (!result.data?.length) return rows;
    rows.push(...(result.data as T[]));
  }
}
