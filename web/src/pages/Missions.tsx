import { Empty, PageHeader } from "../components/ui";

export default function Missions() {
  return (
    <div>
      <PageHeader title="Missions" />
      <Empty title="Being built" hint="This screen is part of the current upgrade." />
    </div>
  );
}
