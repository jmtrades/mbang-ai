import { Field, Input } from "./ui";

export const BRIEF_FIELDS: [string, string, string][] = [
  ["business", "Business", "Peak Physio"],
  ["niche", "Niche", "independent physio clinics"],
  ["audience", "Audience", "clinic owners"],
  ["offer", "Offer", "done-with-you growth programme"],
  ["price", "Price", "3000"],
  ["outcome", "Outcome", "20 new bookings a month"],
  ["timeframe", "Timeframe", "90 days"],
  ["mechanism", "Mechanism", "referral-first system"],
  ["pain", "Pain they hate", "discounting"],
  ["date", "Webinar date", "next Thursday 7pm"],
];

export function BriefForm({ value, onChange, fields }: { value: Record<string, string>; onChange: (v: Record<string, string>) => void; fields?: string[] }) {
  const list = fields ? BRIEF_FIELDS.filter(([k]) => fields.includes(k)) : BRIEF_FIELDS;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {list.map(([k, label, ph]) => (
        <Field key={k} label={label}>
          <Input value={value[k] || ""} placeholder={ph} onChange={(e) => onChange({ ...value, [k]: e.target.value })} aria-label={label} />
        </Field>
      ))}
    </div>
  );
}
