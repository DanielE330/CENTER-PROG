// Chart.tsx не регистрирует markArea (нужен для подсветки периодов дефицита) — регистрируем здесь.
import { use } from "echarts/core";
import { MarkAreaComponent } from "echarts/components";

use([MarkAreaComponent]);
