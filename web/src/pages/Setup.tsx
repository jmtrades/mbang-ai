import { Empty, PageHeader } from "../components/ui";

export default function Setup() {
  return (
    <div>
      <PageHeader title="Setup" />
      <Empty title="Being built" hint="This screen is part of the current upgrade." />
    </div>
  );
}
