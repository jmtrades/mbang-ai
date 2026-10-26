import { Empty, PageHeader } from "../components/ui";

export default function Queue() {
  return (
    <div>
      <PageHeader title="Queue" />
      <Empty title="Not built yet" hint="This screen lands in a later build phase." />
    </div>
  );
}
