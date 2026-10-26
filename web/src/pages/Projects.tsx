import { Empty, PageHeader } from "../components/ui";

export default function Projects() {
  return (
    <div>
      <PageHeader title="Projects" />
      <Empty title="Not built yet" hint="This screen lands in a later build phase." />
    </div>
  );
}
