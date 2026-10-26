import { Empty, PageHeader } from "../components/ui";

export default function Diagnostic() {
  return (
    <div>
      <PageHeader title="Diagnostic" />
      <Empty title="Not built yet" hint="This screen lands in a later build phase." />
    </div>
  );
}
