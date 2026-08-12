const localDateTimePattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/;

function padDatePart(value: number): string {
  return String(value).padStart(2, "0");
}

function normalizeUtcIso(value: string | null | undefined): string {
  if (!value) {
    return "";
  }

  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

export function utcIsoToLocalInputValue(
  value: string | null | undefined,
): string {
  const normalizedValue = normalizeUtcIso(value);
  if (!normalizedValue) {
    return "";
  }

  const date = new Date(normalizedValue);
  return (
    [
      date.getFullYear(),
      padDatePart(date.getMonth() + 1),
      padDatePart(date.getDate()),
    ].join("-") +
    `T${padDatePart(date.getHours())}:${padDatePart(date.getMinutes())}`
  );
}

export function localInputValueToUtcIso(
  value: string,
  previousUtcIso?: string | null,
): string {
  const match = localDateTimePattern.exec(value);
  if (!match) {
    return "";
  }

  const previousIso = normalizeUtcIso(previousUtcIso);
  if (previousIso && utcIsoToLocalInputValue(previousIso) === value) {
    return previousIso;
  }

  const [, yearValue, monthValue, dayValue, hourValue, minuteValue] = match;
  const year = Number(yearValue);
  const month = Number(monthValue);
  const day = Number(dayValue);
  const hour = Number(hourValue);
  const minute = Number(minuteValue);

  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31 ||
    hour > 23 ||
    minute > 59
  ) {
    return "";
  }

  const date = new Date(year, month - 1, day, hour, minute, 0, 0);
  if (
    Number.isNaN(date.getTime()) ||
    date.getFullYear() !== year ||
    date.getMonth() !== month - 1 ||
    date.getDate() !== day
  ) {
    return "";
  }

  return date.toISOString();
}
