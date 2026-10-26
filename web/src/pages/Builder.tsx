import { Empty, PageHeader } from "../components/ui";

export default function Builder() {
  return (
    <div>
      <PageHeader title="Builder" />
      <Empty title="Not built yet" hint="This screen lands in a later build phase." />
    </div>
  );
}
