/**
 * Removes nullish values at runtime while returning Partial<T> because keys can be omitted.
 */
export function omitNullish<T extends object>(value: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(value).filter(
      ([, entryValue]) => entryValue !== null && entryValue !== undefined,
    ),
  ) as Partial<T>;
}
