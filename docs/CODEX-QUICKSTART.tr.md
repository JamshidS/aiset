# Codex ile hızlı başlangıç

Gerekenler: Python 3.9+, Git ve hesabınla giriş yaptığın güncel Codex CLI. TypeScript kod indeksi için Node.js gerekir.
Bu değişiklik henüz PyPI'da yayımlanmadığından Aiset kaynak klasöründe bir kez `pip install -e .` çalıştır.

**1. Projeni takıma dönüştür:**

```powershell
aiset-init "C:\projeler\magaza" --team magaza --provider codex
```

**2. PM ile konuş:**

```powershell
magaza-pm
```

Komut PATH'te değilse `aiset pm --workspace magaza` kullan. İlk mesajın şu olabilir:
“Bilgi notlarını oluştur. Mimariyi, geliştirme kurallarını ve test komutlarını ilgili ajanlara incelet.”

**3. Başka terminalde paneli aç:**

```powershell
aiset
```

[Office](http://127.0.0.1:8765/) ajanların durumunu, dosya hareketlerini, görevleri ve token kullanımını gösterir.
Brain bilgi ve dosya ilişkilerini gösterir. Settings'te model alanını boş bırakınca atamaların modelini PM seçer;
ana PM oturumu kendi Codex varsayılanınla başlar. Bir model veya reasoning değeri sabitlersen PM buna uyar.
Ucuz test modeli, normal ürünün varsayılanı değildir. Maliyetler abonelik faturası değil API referans tahminidir.

**Bir defalık hook onayı:** PM içinde `/hooks` açıp Aiset hook'unu incele ve güvenilir olarak işaretle.
Kod indeksi önerileri ve issue akışındaki yayın kontrolü bunu kullanır. Manuel roster düzenlemesi gerekmez.

**GitHub issue üzerinde çalışmak için:** GitHub CLI (`gh`) kurulu ve hesabına giriş yapılmış olmalı.
İlgili deponun klasöründe `aiset run 20` çalıştır. Önce kabul kriterlerini onaylarsın; ekip çalıştıktan sonra
diff'i inceler, ayrı bir onayla commit/push/taslak PR adımına geçersin. Onay vermezsen yayın yapılmaz.
`--dry-run` önizler; `--resume` aynı Codex oturumuyla devam eder.

Ortak hafıza `.claude/team/` altında kalır; Codex ajanları `.codex/agents/` içinde oluşturulur.
[Ayrıntılı rehber](USER-GUIDE.md#codex-cli-preview), desteklenen kayıt biçimlerini ve Codex hook hatalarında
Claude'dan farklı davranan durumları açıklar. Bu sürüm CLI entegrasyonudur; gelecekteki her native Codex
özelliğinin veya Claude hook davranışının otomatik olarak eşdeğer olduğu anlamına gelmez.
