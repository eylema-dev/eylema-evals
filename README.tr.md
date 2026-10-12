# eylema-evals

Davranış, performans ve donanım değerlendirmesi. [English](README.md).

**Durum: değerlendirme araçları ve sürümlenmiş gözlemler.** G18 sabit yerel görev ölçüm aracı ve üç denemelik M4 sonucu sağlar. G22 sınırlı birleşik Metal/WebGPU ölçüm aracı ve kesin kaynak/artifact profili ekler; gerçek Chrome ortak yük kabulü beklemektedir. Üretim yürütmesi çekirdekte kalır; burada model veya eğitim koşusu sunulmaz.

## Kapsam

Sürümlenmiş sentetik örnekler, iş yükleri, test araçları ve açık sonuç özetleri. Donanım, kod/model/veri revizyonları, sınırlar, başarısızlıklar ve kaynak kullanımı kaydedilir; hızdan önce kalite karşılaştırılır.

Üretim Rust kodu, ortak tipler, çalışma zamanı/model yürütmesi ve CLI [eylema](https://github.com/eylema-dev/eylema) içinde; ürün sözleşmeleri [İngilizce](https://github.com/eylema-dev/eylema-docs-en) ve [Türkçe](https://github.com/eylema-dev/eylema-docs-tr) belge depolarındadır. [Depo haritası](https://github.com/eylema-dev/devguide/blob/main/docs/tr/repository-map.md) sınırları tanımlar.

## Değerlendirme kapıları

[G18 yerel profili](profiles/m4-native-task-v1.json), [iş yükü](workloads/native-task-demo-v1.json) ve [ham sonuç](results/m4-native-task-v1.json), kabul edilmiş CPU görev gözlemini kesin çekirdek derlemesine bağlar. Bu profil Metal, WebGPU, model ve enerjiyi ölçülmemiş bırakır.

[G22 birleşik profili](profiles/m4-combined-gpu-v1.json), [iş yükü](workloads/combined-gpu-v1.json) ve [ölçüm aracı](tools/run_combined_measurement.py), tek sınırlı yerel Metal süreci kesin tamsayı geri okumalarını kontrol ederken üç adet 120 karelik Chrome denemesini tanımlar. Profil çekirdek commit/ağaç, native/CLI/tarayıcı artifactleri ve iş yükü baytlarını sabitler. Temiz, sabitlenmiş çekirdeği kaydedilen komutla yeni ve yalıtılmış Cargo hedef dizinine derleyin. Oluşan CLI hizmetini özel `EYLEMA_DATA_DIR` ile başlatın; yalnızca gözlenen loopback `/console` URL'sini kullanın.

Aracın `collect` komutunu `--core-root`, `--worker`, `--cli`, `--profile`, `--console-url` ve `--private-dir` ile çalıştırın. İzin verilmiş gerçek Chrome kontrolüyle `/console#g22=120&trial=1` adresinden deneme 3'e kadar her görünümü açın, görünür JSON'u `browser-1.json` ile `browser-3.json` dosyalarına kaydedin ve her sekmeyi gerçekten kapatın. `mark --trial <1..3> --phase <opened|closed> --tab-id <observed-id>` bu gözlemleri kaydeder; kendisi sekme açmaz veya kapatmaz. Native iş sürdükten sonra `stop` eşgüdümlü sonlandırma ister. `finalize --private-dir ... --profile ... --output ...` ham örnekleri ve hataları korur; yalnızca örtüşen doğrulanmış Metal işi, gerçek süreç RSS'i ve her kapanıştan sonra aynı PID'nin yeni işi bulunan üç denemeyi kabul eder.

Animasyon callback aralıkları ve kuyruk tamamlanma süreleri ana makine gözlemleridir; donanım GPU veya sunum zaman damgaları değildir. Tarayıcı JS yığını isteğe bağlı tahmindir; bulunmayan değerler null kalır. Native RSS ayrı tarayıcı sürecini kapsamaz. Fiziksel VRAM/sürücü ayırmaları, enerji ve modeller açıkça ölçülmemiş kalır. Dağılımlar en yakın sıra p50/p95/p99 kullanır ve ham örnekleri korur; onaylanmış performans SLA'sı yoktur. Sentetik regresyon örnekleri donanım kabulü sağlamaz. Kaydetmeden önce açık kayıtları özel yollar, kimlik bilgileri ve ilgisiz veri açısından inceleyin.

Python uygulama çalışma zamanı değil, geliştirme/değerlendirme aracıdır. Depo belge kaynak profilini ve ortak kapıları korur; toplayıcı/doğrulayıcı regresyonları olağan Python CI'ında çalışır. Rust/GPU yürütmesi sabitlenmiş birinci taraf çekirdeğe aittir ve ayrı donanım kanıtı gerektirir.

[Katkı rehberini](CONTRIBUTING.tr.md) ve [AGENTS](AGENTS.md) sözleşmesini kullanın. Özel veriler, kimlik bilgileri, operasyon kayıtları ve model ağırlıkları Git dışında kalır. Açık varlıklar için ayrıca onaylanmış depolama, köken ve lisans kararı gerekir; burada depolama veya hizmet kurulmaz. Kendi kaynak kodumuz ve belgelerimiz [Apache-2.0](LICENSE) lisanslıdır. Ayrı belirtilen üçüncü taraf içerikleri, model ağırlıkları ve veri kümeleri kendi koşullarını korur.
