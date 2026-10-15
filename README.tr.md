# Eyleme değerlendirmeleri

**Tek Eyleme markası — 15 Ekim 2026:** [eyleme.dev](https://eyleme.dev/tr/), Eyleme ve topluluğunun ortak merkezidir. Eyleme bağımsız kullanılabilen, public ve Apache-2.0 lisanslı bir proje olarak kalır. **Eyleme Derleme** ve **Eyleme Onama**, isteğe bağlı ürünler olarak private kaynak kodlu, ücretli abonelik modelini izler. Ticari hizmetler ve koşullar geliştirme aşamasındadır; fiyatlar ve kullanıma sunulma durumu yayımlanmamıştır. Bu depo abonelik hizmeti sunmaz.

Ürün ailesi **Derleme → Onama → Eyleme** sırasıyla anlatılır: veriyi hazırla, işi yetkilendir, ardından yürüt. Eyleme kendi temel erişim kontrollerini korur ve bağımsız kullanım için iki aboneliğe de ihtiyaç duymaz. [Public ürün ailesi açıklamasına](https://github.com/eyleme-dev/eyleme/blob/main/docs/tr/product-family.md) bakın.

Teknik tanımlayıcılar uyumluluk için mevcut yazımlarını korur. Tarihsel kanıtlar özgün ad ve bağlantılarını korur.

Davranış, performans ve donanım değerlendirmesi. [English](README.md).

**Durum: değerlendirme araçları ve sürümlenmiş gözlemler.** G18 sabit yerel görev ölçüm aracı ve üç denemelik M4 sonucu sağlar. G22 sınırlı birleşik Metal/WebGPU ölçüm aracı, kesin kaynak/artifact profili ve üç gerçek Chrome/M4 gözlemi ekler. Ham kayıtlar önceki başarısız girişimi korur; teslim kapıları donanım gözlemlerinden ayrıdır. Üretim yürütmesi çekirdekte kalır; burada model veya eğitim koşusu sunulmaz.

## Kapsam

Sürümlenmiş sentetik örnekler, iş yükleri, test araçları ve açık sonuç özetleri. Donanım, kod/model/veri revizyonları, sınırlar, başarısızlıklar ve kaynak kullanımı kaydedilir; hızdan önce kalite karşılaştırılır.

Üretim Rust kodu, ortak tipler, çalışma zamanı/model yürütmesi ve CLI [Eyleme](https://github.com/eyleme-dev/eyleme) içinde; ürün sözleşmeleri [İngilizce](https://github.com/eyleme-dev/eyleme-docs-en) ve [Türkçe](https://github.com/eyleme-dev/eyleme-docs-tr) belge depolarındadır. [Depo haritası](https://github.com/eyleme-dev/devguide/blob/main/docs/tr/repository-map.md) sınırları tanımlar.

## Değerlendirme kapıları

[G18 yerel profili](profiles/m4-native-task-v1.json), [iş yükü](workloads/native-task-demo-v1.json) ve [ham sonuç](results/m4-native-task-v1.json), kabul edilmiş CPU görev gözlemini kesin çekirdek derlemesine bağlar. Bu profil Metal, WebGPU, model ve enerjiyi ölçülmemiş bırakır.

[G22 birleşik profili](profiles/m4-combined-gpu-v1.json), [iş yükü](workloads/combined-gpu-v1.json) ve [ölçüm aracı](tools/run_combined_measurement.py), tek sınırlı yerel Metal süreci kesin tamsayı geri okumalarını kontrol ederken üç adet 120 karelik Chrome denemesini tanımlar. Profil çekirdek commit/ağaç, native/CLI/tarayıcı artifactleri ve iş yükü baytlarını sabitler. Temiz, sabitlenmiş çekirdeği kaydedilen komutla yeni ve yalıtılmış Cargo hedef dizinine derleyin. Oluşan CLI hizmetini özel `EYLEMA_DATA_DIR` ile başlatın; yalnızca gözlenen loopback `/console` URL'sini kullanın.

Aracın `collect` komutunu `--core-root`, `--worker`, `--cli`, `--profile`, `--console-url` ve `--private-dir` ile çalıştırın. İzin verilmiş gerçek Chrome kontrolüyle önce olağan yerel `/console` sekmesini açın ve ölçüm başlamadan önce gerçek kimliğini `mark --phase opened` ile kaydedin. Aynı sekmeyi `/console#g22=120&trial=1` adresinden deneme 3'e kadar yönlendirin ve sınırlı ölçümü başlatmak için yenileyin; görünür JSON'u `browser-1.json` ile `browser-3.json` dosyalarına kaydedin ve her sekmeyi gerçekten kapatın. `mark --trial <1..3> --phase <opened|closed> --tab-id <observed-id>` bu gözlemleri kaydeder; kendisi sekme açmaz veya kapatmaz. Native iş sürdükten sonra `stop` eşgüdümlü sonlandırma ister. `finalize --private-dir ... --profile ... --output ...` ham örnekleri ve hataları korur; yalnızca örtüşen doğrulanmış Metal işi, gerçek süreç RSS'i ve her kapanıştan sonra aynı PID'nin yeni işi bulunan üç denemeyi kabul eder. Açılış gözlemi ilk kareden önce, kapanış gözlemi son kareden sonra olmalıdır.

Animasyon callback aralıkları ve kuyruk tamamlanma süreleri ana makine gözlemleridir; donanım GPU veya sunum zaman damgaları değildir. Tarayıcı JS yığını isteğe bağlı tahmindir; bulunmayan değerler null kalır. Native RSS ayrı tarayıcı sürecini kapsamaz. Fiziksel VRAM/sürücü ayırmaları, enerji ve modeller açıkça ölçülmemiş kalır. Dağılımlar en yakın sıra p50/p95/p99 kullanır ve ham örnekleri korur; onaylanmış performans SLA'sı yoktur. Sentetik regresyon örnekleri donanım kabulü sağlamaz. Kaydetmeden önce açık kayıtları özel yollar, kimlik bilgileri ve ilgisiz veri açısından inceleyin.

Python uygulama çalışma zamanı değil, geliştirme/değerlendirme aracıdır. Depo belge kaynak profilini ve ortak kapıları korur; toplayıcı/doğrulayıcı regresyonları olağan Python CI'ında çalışır. Rust/GPU yürütmesi sabitlenmiş birinci taraf çekirdeğe aittir ve ayrı donanım kanıtı gerektirir.

[Katkı rehberini](CONTRIBUTING.tr.md) ve [AGENTS](AGENTS.md) sözleşmesini kullanın. Özel veriler, kimlik bilgileri, operasyon kayıtları ve model ağırlıkları Git dışında kalır. Açık varlıklar için ayrıca onaylanmış depolama, köken ve lisans kararı gerekir; burada depolama veya hizmet kurulmaz. Kendi kaynak kodumuz ve belgelerimiz [Apache-2.0](LICENSE) lisanslıdır. Ayrı belirtilen üçüncü taraf içerikleri, model ağırlıkları ve veri kümeleri kendi koşullarını korur.

G22 kayıtları: [üç başarılı deneme](results/m4-combined-gpu-v1.json), [korunan ilk başarısız girişim](results/m4-combined-gpu-v1-attempt-1.json) ve [yakalama kökeni ve sınırları](results/m4-combined-gpu-v1-observation.json). Her deneme 120 tarayıcı karesi ve 98/89/90 örtüşen doğrulanmış Metal çağrısı içerir. Aynı native PID, her gerçek sekme kapanışından sonra beş yeni doğrulanmış çağrı tamamlar. RAF p95 9,2/9,3/9,2 ms; tam native çağrı p95 6,587/8,357/7,790459 ms oldu. Bu ana makine gözlemleri yalnızca GPU süresi, fiziksel VRAM değeri veya performans SLA'sı iddiası değildir. AX metni kesildiği için gerçek Chrome native arayüzü, tam JSON çıkarımı için DOM görüntülerini kaydetti; görüntüdeki sekme indeksleri denemeyle sınırlıdır ve yeniden kullanılabilir. Kaydedilmiş HTML veya ilgisiz tarayıcı verisi yayımlanmaz.
