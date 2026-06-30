# Phase B — live selector probe

## tunisianet
  sample: https://www.tunisianet.com.tn/pc-portable-tunisie/87603-pc-portable-bmax-maxbook-x15-pro-n95-16-go-512-go-ssd-windows-11-gris.html
  BROKEN selectors: brand_img, price_attr, old_price, discount, stock, description, main_image, gallery_images
    suggest title: `h1[itemprop="name"]` -> PC PORTABLE BMAX MAXBOOK X15 PRO / N95 / 16 GO / 512 GO SSD / WINDOWS 11 / GRIS
    suggest price: `[itemprop="price"]` -> 1 099,000 DT
    suggest sku: `[itemprop="sku"]` -> MAXBOOK-X15PRO
    suggest desc: `[itemprop="description"]` -> Écran 15.6" Full HD, IPS - Processeur Intel N95 12e génération, (jusqu’à 3.4 GHz
    suggest avail: `.stock` -> Disponible

## affariyet
  sample: https://www.affariyet.com/pc-de-bureau/mini-pc-de-bureau-bmax-b1-pro-n4000-8go-128go-ssd-.html
  BROKEN selectors: price, description, images, image_attrs
    suggest title: `h1` -> Mini Pc de bureau BMAX B1 PRO N4000 8Go 128Go SSD
    suggest price: `.current-price` -> 529,000 TND TTC
    suggest sku: `.product-reference span` -> BMAX-B1PRO
    suggest avail: `.product-availability` -> check En Stock

## tunewtec
  sample: https://tunewtec.com/s/clavier-macro-bureautique-multimedia-bilingue-k747474/
    suggest title: `h1.entry-title` -> Clavier Macro Bureautique Multimédia Bilingue (K747474)
    suggest price: `.price .amount` -> 8.500 DT
    suggest sku: `.sku` -> K747474
    suggest desc: `.woocommerce-product-details__short-description` -> Clavier: Bilingue
Interface: USB
Bureautique
Multimédia
    suggest avail: `.availability` -> EN STOCK

## koktahome
  sample: https://www.koktahome.com/produits/tv-tcl-mini-led-98-smart-c6k-4k-uhd-98c6k/
  BROKEN selectors: brand, image_main, image_thumbnails
    suggest title: `h1.product_title` -> TV TCL Mini LED 98″ Smart C6K 4K UHD 98C6K
    suggest price: `.price .amount` -> د.ت 11.599
    suggest sku: `.sku` -> 98C6K
    suggest desc: `.woocommerce-product-details__short-description` -> TV TCL  MINI LED 98″ Smart C6K – Taille de l’écran: 98 pouces – Résolution: UHD 
    suggest avail: `.stock` -> En stock

## darty
  sample: https://darty.tn/lave-linge-frontal/2790-lave-linge-105kg-hoover-silver-h3ws4105tcge-8059019054759.html
  BROKEN selectors: old_price, discount, sku, sku_attr, brand, brand_attr, stock, main_image, main_image_attr, gallery_images
    suggest title: `h1[itemprop="name"]` -> LAVE-LINGE FRONTAL Lave linge Hoover 10,5kg silver H3WS4105TCGE
    suggest price: `[itemprop="price"]` -> 1 629,990 DT
    suggest desc: `#description` -> Le lave-linge HOOVER H3WS4105TCGE 10,5 Kg Silver est conçu pour offrir une expér

## bstech
  sample: https://www.bstech.tn/site/product/souris-redragon-bm2559-sans-fil-noir
    suggest title: `h1` -> SOURIS REDRAGON BM2559 SANS FIL BLACK
    suggest desc: `.product-description` -> Souris Redragon BM2559 Sans Fil Black — Périphérique en Tunisie

La Souris Redra

## emh
  sample: https://emh.tn/cuisiniere-/1154-cuisiniere-biolux-5005-blanc-cui-5005-cuisiniere-biolux.html
    suggest title: `h1` -> emh.tn

## sbs
  sample: https://www.sbsinformatique.com/pc-gamer-tunisie/pc-gamer-raptor-ryzen-5-3400g-16gb-240gb-tunisie
  BROKEN selectors: price_content_attr, old_price, brand, brand_attr, availability_schema, image_main, image_thumbnails
    suggest title: `h1[itemprop="name"]` -> PC Gamer RAPTOR - Ryzen 5 3400G - 16Gb - 240Gb
    suggest price: `[itemprop="price"]` -> 999,000 TND
    suggest sku: `[itemprop="sku"]` -> 3400G-16-240-RP
    suggest desc: `#description` -> Le meilleur rapport qualité-prix pour commencer le gaming en Tunisie

Vous cherc
    suggest avail: `#product-availability` -> En stock

## jmb
  sample: https://jmb.com.tn/micro-casque-usb-tucci-q5-noir
  BROKEN selectors: image_gallery
    suggest title: `h1.product_title` -> MICRO CASQUE USB TUCCI Q5 – NOIR
    suggest price: `.product-price .price` -> 35,000 TND
    suggest sku: `.sku` -> TUCCI Q5
    suggest desc: `.woocommerce-product-details__short-description` -> Micro casque TUCCI Q5 – Technologie de connectivité: Filaire – Interface: USB – 
    suggest avail: `.in-stock` -> En stock

## scoop
  sample: https://www.scoopgaming.com.tn/13750-pc-gamer-alpha-valorant-i5-12eme-rtx-3050-ventus-2x-16go-512g-ssd.html
  BROKEN selectors: price_content_attr, old_price, brand, brand_attr, availability_schema, image_main, image_thumbnails
    suggest title: `h1[itemprop="name"]` -> Pc Gamer ALPHA Valorant, I5-12ème, RTX 3050 Ventus 2X, 16Go, 500G SSD
    suggest price: `[itemprop="price"]` -> 2 499,000 TND
    suggest sku: `[itemprop="sku"]` -> valorant-recomd
    suggest desc: `#description` -> PC SUR MESURE 

Le PC monté ALPHA VALORANT combine style, réactivité et puissanc
    suggest avail: `#product-availability` -> Sur Commande 48h

## taktek
  ERROR: net::ERR_CERT_DATE_INVALID at https://taktek.com.tn/telephonie-tunisie/t%C3%A9l%C3%A9phone-portable-tunisie/telephone-portable-smartec-s24.html

## wiki
  sample: https://wiki.tn/smartphone-zte-blade-a33s-4g-232go-bleu-2/
  BROKEN selectors: brand, brand_attr, image_main, image_gallery
    suggest title: `h1` -> ZTE Blade A33s – Smartphone 4G 2+32Go Bleu
    suggest price: `.price .amount` -> 299,00 TND
    suggest sku: `.sku` -> ZTEA33S-32BL
    suggest desc: `.woocommerce-product-details__short-description` -> Écran: 6.3″ LCD IPS 
Résolution: 1014 x 480 Pixels 
OS: Android S (Go edition)
C
    suggest avail: `.stock` -> En stock

## techland
  sample: https://techland.tn/produit/souris-sans-fil-inca-img-326mx-programmable-rechargeable-noir
    suggest title: `h1` -> Souris Sans Fil INCA IMG-326MX Programmable Rechargeable - Noir

## electrobennjima
  sample: https://electrobennjima.tn/hachoir-a-viande-moulinex-2000w/
    suggest title: `h1.product_title` -> HACHOIR A VIANDE MOULINEX – 2000W
    suggest price: `.price .amount` -> د.ت 429,000
    suggest sku: `.sku` -> PEME620132
    suggest desc: `#tab-description` -> Description
Référence	PEME620132
Marque	MOULINEX
Garantie	1 an
Puissance	2000 W

    suggest avail: `.stock` -> Available in stock

## acspace
  sample: https://acspace.tn/pack-cuisine-forsa-05-pieces-electromenager/
  BROKEN selectors: price_sale, price_original, availability_in_stock, availability_out, specs_container, specs_key, specs_value, image_gallery
    suggest title: `h1` -> Pack Mariage – 05 Pcs Électroménager
    suggest price: `.price .amount` -> 3689.000 TND
    suggest sku: `.sku` -> PackForsa
    suggest desc: `.woocommerce-product-details__short-description` -> Ce pack Contient :
– Réfrigérateur 291L Hyundai HYN-186.60NF.B – combine – nofro

## chaktech
  sample: https://chaktech.tn/shop/imprimante-3-en-1-canon-pixma-g2420-5-bouteilles-dencre/
    suggest price: `.price .amount` -> د.ت 559,000
    suggest sku: `.sku` -> Imprimante-3-en-1-Canon-Pixma-G2420+5-bouteilles-d’encre
    suggest desc: `.woocommerce-product-details__short-description` -> Fonctions: Impression, copie et numérisation
Technologie d’impression: Jet d’enc
    suggest avail: `.stock` -> Rupture de stock

## topbureau
  sample: https://www.topbureau.tn/index.php?route=product/product&path=33&product_id=65
  BROKEN selectors: product_id, old_price, images
    suggest title: `h1` -> KONICA MINOLTA C251i (COPIEUR 25ppm/ IMPRIMANTE / SCANNER COULEUR) AVEC CACHE VI
    suggest price: `[itemprop="price"]` -> 0.000DT
    suggest desc: `[itemprop="description"]` -> Avec les dernières technologiques, le nouveau  Minolta / DEVELOP C251i permet de

## sigshop
  sample: https://sig-shop.tn/produit/telephone-portable-logicom-p-197e-gris/
  BROKEN selectors: availability, image_main, image_thumbnails, image_thumb_attr
    suggest title: `h1.product_title` -> Téléphone PORTABLE LOGICOM P 197E – GRIS
    suggest price: `.price .amount` -> 35 TND
    suggest sku: `.sku` -> LOG-P197E-b
    suggest desc: `.woocommerce-product-details__short-description` -> Écran: 1.77″ (128x160pixels) – Mémoire: 32Mo – Stockage: 32Mo Avec Micro SDHC (j

## ispace
  sample: https://ispaceservices.com/product/ecran-lg-27-4k-led
  BROKEN selectors: sku, old_price, description, specs_rows, variations_form, variation_options, image_gallery
    suggest title: `h1.product_title` -> ECRAN LG 27″ 4K LED
    suggest price: `.price .amount` -> 2 500 DT
    suggest desc: `.woocommerce-product-details__short-description` -> LG 27UQ850V-W écran Plat de PC 68,6 cm (27″) 3840 x 2160 Pixels 4K Ultra HD LCD 
    suggest avail: `.stock` -> En stock

## yatoo
  sample: https://yatoo.com.tn/accessoire-iphone/586-chargeur-wuw-t55-31a-pour-iphone.html
    suggest title: `h1` -> Chargeur WUW T55 3,1A pour IPhone

## qsnet
  sample: https://qsnet.tn/produit/pc-de-bureau-all-in-one-asus-v400-aio-p440vak-core-7-240h-8-go-ddr5-512-go-ssd-noir/
  BROKEN selectors: sku, brand, availability, image_main, image_thumbnails, image_thumb_attr
    suggest title: `h1.product_title` -> PC de Bureau ALL IN ONE ASUS P440VAK Intel Core 7 240H 8Go 512Go SSD
    suggest price: `.price .amount` -> 2.639,000 TND
    suggest sku: `.product-sku` -> SKU: P440VAK-BPC6900
    suggest desc: `.product-description` -> Soyez le premier a donner votre avis sur “PC de Bureau ALL IN ONE ASUS P440VAK I

## sangour
  sample: https://sangour.tn/product/bellaoggi-primer-acqua-boost/
    suggest title: `h1.product_title` -> BELLAOGGI PRIMER ACQUA BOOST
    suggest price: `.price .amount` -> 9.280 TND
    suggest sku: `.sku` -> 8028997106880
    suggest desc: `#tab-description` -> ACQUA BOOST est la base de teint gel ultra-hydratante qui se fond avec la peau p

## alarabia
  sample: https://www.alarabia.com.tn/accueil/15143-pc-portable-lenovo-ideapad1-15ijl7-intel-celeron-n4500-8g-256g-ssd-bleu.html
    suggest title: `h1[itemprop="name"]` -> PC PORTABLE LENOVO IDEAPAD1 15IJL7 INTEL CELERON N4500 8G 256G SSD - BLEU
    suggest price: `[itemprop="price"]` -> 879,000 TND
    suggest sku: `[itemprop="sku"]` -> 100001001142
    suggest desc: `[itemprop="description"]` -> Le PC Portable LENOVO IP 82LX00CKFG (IdeaPad 1 15IJL7) dispose d’un écran 15.6" 
    suggest avail: `#product-availability` -> Rupture de stock

## bestbuytunisie
  sample: https://bestbuytunisie.tn/pc-portable-gamer-lenovo-loq15iax9-i5-12gen-12go-512go-ssd-rtx-2050-4go-gris-83gs00skfg-tunisie/
  BROKEN selectors: old_price, image_main, image_thumbnails, image_thumb_attr
    suggest title: `h1.product_title` -> Pc Portable Gamer Lenovo Loq15IAX9 I5 12Gén 12Go 512Go Ssd RTX 2050 4Go- Gris – 
    suggest price: `.price .amount` -> 59.000 DT
    suggest sku: `.sku` -> 165973
    suggest desc: `.woocommerce-product-details__short-description` -> Pc Portable Gamer Lenovo Loq 15iax9
– Ecran : 15.6″ FHD ,144Hz
– Processeur : In
    suggest avail: `.stock` -> Rupture de stock

## informatica
  sample: https://informatica.tn/produit/smartphone-huawei-nova-9-8go-128go-starry-noir-ref-hu-nova9/
    suggest title: `h1.product_title` -> SMARTPHONE HUAWEI NOVA 9 8GO 128GO STARRY NOIR REF HU-NOVA9 – Cadeau Offert :Eco
    suggest price: `.price .amount` -> 1989 DT
    suggest sku: `.sku` -> NOVA 9-1
    suggest desc: `.woocommerce-product-details__short-description` -> Ecran : 6.57 OLED  – Résolution: 1080×2340 Pixels – Système d’exploitation: Harm
    suggest avail: `.stock` -> Rupture de stock

## skymill
  sample: https://www.skymil-shop.com/catalogue/pc-gamer-bureautique/full-setup
  BROKEN selectors: description, image_main
    suggest title: `h1` -> Full Setup

## benzarti-electromenager
  sample: https://benzarti-electromenager.com/boutique/televiseur/hisense-50-a6n-uhd-smart-tv-4k-60-hz
  BROKEN selectors: sku, current_price, old_price, availability, description_meta, image_meta, images, image_attrs
    suggest price: `.price .amount` -> د.ت 0,000

## dokani
  sample: https://www.dokani.tn/shop/linge-de-maison-54/whm26-6-kit-de-deplacement-de-meuble-lourd-8-pieces-8590
  BROKEN selectors: sku, availability, description, image_main, image_thumbnails
    suggest title: `h1` -> Kit De Déplacement De Meuble Lourd- 8 pièces
    suggest price: `.product_price` -> 19.98 DT
22.70 DT
(12% OFF)
    suggest desc: `[itemprop="description"]` -> 🛠️✨ Kit de Déplacement de Meubles avec Levage et Roulettes – Transport Facile & 

## electrochaabani
  sample: https://www.electrochaabani.com/produit/Base-metallique-reglable-KBN-16-pour-Refrigerateur-machine-a-laver-avec-roulettes-2pcs
  BROKEN selectors: title, sku, description, image_main, image_thumbnails

## graiet
  sample: https://www.graiet.tn/biolux-mini-bar-mp-07-70-litres-noir-de-frost.html
  BROKEN selectors: price_attr, currency, currency_attr, brand_link, main_image, main_image_attr, gallery_images
    suggest title: `h1` -> Mini Bar BIOLUX 70 Litres De Frost | MP.07 - Noir
    suggest price: `.price` -> 489,00 TND
    suggest sku: `[itemprop="sku"]` -> 0101391
    suggest desc: `[itemprop="description"]` -> Réfrigérateur Mini-Bar BIOLUX - MP-07   Volume brut : 70 Litres / Refroidissemen
    suggest avail: `.stock` -> En stock

## imag
  sample: https://imag.tn/produit/climatiseur-biolux-m-121-cfts/
  BROKEN selectors: old_price, sku, availability, specs_container, specs_key, specs_value, image_main, image_thumbnails
    suggest title: `h1.product_title` -> Climatiseur BIOLUX 12000 CHAUD/FROID TROPICAL SMART M.121 CFTS
    suggest price: `.price .amount` -> د.ت1,249.000
    suggest sku: `.sku` -> Référence   – M.121 CFTS
    suggest desc: `.woocommerce-product-details__short-description` -> IMAG – La référence en électroménager en Tunisie, avec des produits adaptés à vo

## megapc
  sample: https://megapc.tn/shop/product/ORDINATEURS/PC%20GAMER/Astro-4X-Intel-Core-i7-12700K--RX-9060-XT-16GB-32GB-500GB-NVMe
    suggest title: `h1` -> Astro 4X 🧑‍🚀 | Intel Core i7-12700K | RX 9060 XT 16GB | 32GB | 500GB NVMe

## techgate
  sample: https://techgate.tn/produit/telephone-portable-nokia-n130-dark-bleu/
  BROKEN selectors: old_price, brand, availability_out, image_main, image_gallery
    suggest title: `h1.product_title` -> TELEPHONE PORTABLE NOKIA N130 DARK BLEU
    suggest price: `.price .amount` -> 109,000 DT
    suggest sku: `.sku` -> NOKIA-N130-BLEU
    suggest desc: `.woocommerce-product-details__short-description` -> Double SIM – Écran: 2.4″ QVGA – Système : S30+ – Mémoire: 4Mo – Stockage: Prise 
    suggest avail: `.stock` -> En stock (peut être commandé)
