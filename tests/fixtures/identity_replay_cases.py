"""Real Brave results from the identity replay of 2026-08-28.

Captured by replaying every historical item against the live backend, and kept
because the numbers they produced are the reason exact resolution is not shipped:
the corroboration rule resolved 13 of 54 items and **six of those thirteen were
wrong**.

These are not illustrations. Each entry is the exact URL-and-title list the backend
returned for that item, so a future exact-identity rule can be measured against the
cases that defeated this one rather than against invented ones.

`should_resolve` is a human judgement of what a correct rule ought to conclude,
made by reading the hits against the item. It is the target, not a description of
current behaviour -- today every case resolves to False, because exact resolution
fails closed.

The one to keep in view is MP-000003. A Brooks Brothers suit jacket whose style
code reduced to the fragment `2bsv`, resolved by three independent plumbing
suppliers as a Barmesa submersible sewage pump. Any rule that resolves that case is
not ready to lift a pricing ceiling.

The corpus deliberately contains failures in both directions. MP-000018 is an ISBN
with a valid check digit and nineteen sources that are unanimously the same book,
and the rule rejected it. A rule that is wrong in both directions is not "too
strict" or "too lenient"; it is measuring the wrong thing.
"""

from __future__ import annotations

CASES = [
    {
        "sku": "MP-000003",
        "identifier": "SUJT EXP 2BSV SLIM",
        "item": "Brooks Brothers Explorer Slim suit jacket",
        "query": "2bsv",
        "should_resolve": False,
        "why": "the code token '2bsv' matched a line of Barmesa submersible sewage pumps; three independent plumbing suppliers agreed on barmesa/pump/sewage/stainless",
        "hits": [
            [
                "https://www.pollardwater.com/product/barmesa-pumps-bsv-series-2-in-2-hp-230v-single-phase-316-stainless-steel-submersible-sewage-pump-b2bsv202ds/_/R-8511206",
                "Barmesa Pumps BSV Series 2 in. 2 hp 230V Single Phase 316 Stainless Steel Submersible Sewage Pump - 2BSV-202DS - Pollardwater"
            ],
            [
                "https://www.absolutewaterpumps.com/barmesa-submersible-stainless-steel-vortex-sewage-pump-2bsv-304ds-2-179-gpm-3-0-hp-3-phase-460-volt",
                "Barmesa Submersible Stainless Steel Vortex Sewage Pump - 2BSV-304DS, 2\", 179 GPM, 3.0 HP, 3-Phase, 460 Volt"
            ],
            [
                "https://rcworst.com/products/barmesa-2bsv-202ds-submersible-stainless-vortex-sewage-pump-2-0-hp-230v-1ph-33-cord-manual",
                "Barmesa Pumps - Barmesa 2BSV-202DS Submersible Stainless Vortex Sewage Pump 2.0 HP 230V 1PH 33' Cord Manual #BMS2BSV202DS"
            ]
        ]
    },
    {
        "sku": "MP-000005",
        "identifier": "A3211",
        "item": "Beats Pill+ speaker, no brand on the identification",
        "query": "A3211",
        "should_resolve": True,
        "why": "the right product IS among the hits, but a bare short code also matched a New Jersey senate bill, an Aegean Airlines flight and an Allegro microchip",
        "hits": [
            [
                "https://www.allegromicro.com/-/media/files/datasheets/a3211-12-datasheet.pdf",
                "DFN (EH) The A3211 and A3212 integrated circuits are ultrasensitive, pole"
            ],
            [
                "https://www.njleg.state.nj.us/bill-search/2022/A3211",
                "Bill A3211"
            ],
            [
                "https://www.flightaware.com/live/flight/AEE211",
                "A3211 (AEE211) Aegean Airlines Flight Tracking and History - FlightAware"
            ],
            [
                "https://fccid.io/BCGA3211",
                "Apple . Beats Pill A3211 FCC ID BCGA3211"
            ],
            [
                "https://shop.warehousewireless.com/product/beats-pill-2024-a3211-black---dwkq47mwm6",
                "Beats Pill (2024) (A3211) Black - - - Warehouse Wireless"
            ],
            [
                "https://www.vtechhotelphones.com/pd/4815/NG-A3211-Silver-Black-1-Line-Analog-Corded-Phone",
                "1-Line Analog Corded Phone NG-A3211 Silver Black"
            ],
            [
                "https://www.alldatasheet.com/datasheet-pdf/pdf/143413/ALLEGRO/A3211.html",
                "A3211 Datasheet(PDF) - Allegro MicroSystems"
            ],
            [
                "https://www.pawnamerica.com/Product/Beats_Pill_A3211_Portable_IP67_Rated_USB_C_Wireless_Bluetooth_Speaker_25123065998",
                "Beats Pill A3211 Portable IP67 Rated USB-C Wireless Bluetooth Speaker"
            ],
            [
                "https://appledb.dev/device-selection/Beats-Speakers.html",
                "Device Selection (Beats Speakers) | AppleDB"
            ],
            [
                "https://cdn-web.vtp-media.com/products/NG/NG-A3211/NG-A3211_UG_V1_CEC_US_2022.02.28.pdf",
                "Analog Next Gen Series NG-A3211 Analog Next Gen Corded 1-line Hotel Telephone"
            ],
            [
                "https://www.gowithdfw.com/products/phone-ng-a3211-corded-1-line-color-pearl-black",
                "NG-A3211 Pearl and Black 1-Line Analog Corded Phone with Speakerphone \u2013 DFW Motel Supply"
            ],
            [
                "https://www.nysenate.gov/legislation/bills/2019/A3211",
                "NY State Assembly Bill 2019-A3211"
            ],
            [
                "https://hdsupplysolutions.com/p/vtech-ng-a3211-1-line-analog-corded-hotel-guestroom-telephone-p234620",
                "Vtech Ng-A3211 1-Line Analog Corded Hotel Guestroom Telephone | HD Supply"
            ],
            [
                "https://www.reddit.com/r/beatsbydre/comments/1fm2a3m/latest_firmware_of_beats_pill_a3211/",
                "r/beatsbydre on Reddit: Latest Firmware of Beats Pill A3211?"
            ]
        ]
    },
    {
        "sku": "MP-000010",
        "identifier": "SelectTech",
        "item": "Bowflex SelectTech 552 dumbbells",
        "query": "Bowflex SelectTech",
        "should_resolve": False,
        "why": "'SelectTech' is the product line; the 552 and the 1090 are different products at roughly twice the price",
        "hits": [
            [
                "https://www.bowflex.com/adjustable-weights/",
                "Explore SelectTech Adjustable Weights \u2013 BowFlex"
            ],
            [
                "https://www.costco.com/p/-/bowflex-selecttech-552-dumbbells-with-stand/100683544",
                "Bowflex SelectTech 552 Dumbbells With Stand | Costco"
            ],
            [
                "https://www.amazon.com/stores/BowFlex/page/85149DDB-217B-4AD6-A377-D4DDE14C75FF",
                "Amazon.com: BowFlex: SelectTech"
            ],
            [
                "https://www.garagegymreviews.com/bowflex-selecttech-552-adjustable-dumbbells-review",
                "Bowflex SelectTech 552 Review 2026 | Garage Gym Reviews"
            ],
            [
                "https://www.dickssportinggoods.com/p/bowflex-selecttech-52-dumbbell-pair-26bowufitnslcttch5pvs/26bowufitnslcttch5pvs",
                "Bowflex SelectTech 52 Dumbbell \u2013 Pair | Dick's Sporting Goods"
            ],
            [
                "https://www.academy.com/p/bowflex-selecttech-52-adjustable-dumbbell-pair",
                "Bowflex SelectTech 52 Adjustable Dumbbell Pair | Academy"
            ],
            [
                "https://thebowflex.com/",
                "Bowflex\u00ae Dumbbells | Adjustable - Official Website"
            ],
            [
                "https://www.martinsbike.com/product/bowflex-selecttech-552-dumbbells-15121.htm",
                "Bowflex SelectTech 552 Dumbbells - Martins Bike & Fitness"
            ],
            [
                "https://www.menshealth.com/fitness/a69060320/bowflex-adjustable-dumbbells-review/",
                "BowFlex Results Series SelectTech 552 Review: Using Them at Home"
            ],
            [
                "https://www.ign.com/articles/bowflex-selecttech-adjustable-dumbbell-deal-amazon-prime-day-sale",
                "Save $100 Off the BowFlex SelectTech Adjustable Dumbbells for Prime Day. Bowflex Is Still the Gold Standard That All Other Adjustable Dumbbells Are Compared To."
            ]
        ]
    },
    {
        "sku": "MP-000017",
        "identifier": "DJI Osmo",
        "item": "DJI Osmo Action 5 Pro",
        "query": "DJI Osmo",
        "should_resolve": False,
        "why": "'DJI Osmo' is the line; the confirming pages are Osmo Pocket 3, Osmo 360 and a Wikipedia overview -- none of them this camera",
        "hits": [
            [
                "https://www.dji.com/osmo-pocket-3",
                "Osmo Pocket 3 - For Moving Moments - DJI United States"
            ],
            [
                "https://en.wikipedia.org/wiki/DJI_Osmo",
                "DJI Osmo - Wikipedia"
            ],
            [
                "https://www.cnet.com/tech/computing/dji-osmo-360-camera-review-impressive-hardware-but-theres-a-catch/",
                "DJI Osmo 360 Camera Review: Impressive Hardware, but There's a Catch - CNET"
            ],
            [
                "https://www.dpreview.com/articles/djis-osmo-pocket-4p-is-here-these-videos-explain-it/",
                "DJI's Osmo Pocket 4P is here. These videos explain it. | DPReview"
            ],
            [
                "https://www.amazon.com/DJI-Stabilization-Rotatable-Touchscreen-Photography/dp/B0CG19QXWD",
                "Amazon.com : DJI Osmo Pocket 3 Vlogging Camera with 1'' CMOS & 4K/120fps Video | 3-Axis Stabilization, Fast Focusing, Spotlight Follow, 2\" Rotatable Touchscreen, Video Camera Camcorder for Photography : Electronics"
            ],
            [
                "https://aerial-guide.com/article/dji-osmo-pocket-review-pros-amp-cons",
                "DJI Osmo Pocket Review | Pros & Cons \u2014 Aerial Guide"
            ]
        ]
    },
    {
        "sku": "MP-000048",
        "identifier": "COMMANDER",
        "item": "NERF Elite 2.0 Commander RD-6",
        "query": "NERF COMMANDER",
        "should_resolve": True,
        "why": "borderline: the hits are overwhelmingly the right blaster, but the only agreed word was 'nerf', which the query itself supplied",
        "hits": [
            [
                "https://www.amazon.com/NERF-Commander-Official-Rotating-Attachment/dp/B083QZLSXS",
                "Amazon.com: NERF Elite 2.0 Commander RD-6 Dart Blaster, 12 Darts, 6-Dart Rotating Drum, Outdoor Toys, Ages 8 and Up : Toys & Games"
            ],
            [
                "https://nerf.fandom.com/wiki/Commander_RD-6",
                "Commander RD-6 | Nerf Wiki | Fandom"
            ],
            [
                "https://www.walmart.com/ip/NERF-ELITE-COMMANDER/206284954",
                "Nerf Elite 2.0 Commander RD-6 Blaster, 12 Darts, 6-Dart Drum, Outdoor Kids Toys, Ages 8+ - Walmart.com"
            ],
            [
                "https://www.nerfcommander.com/",
                "Nerf Commander | Mobile Nerf Battlefield | Columbia, South Carolina"
            ],
            [
                "https://blasterhub.com/2021/05/quick-review-nerf-elite-2-0-commander/",
                "Quick Review: Nerf Elite 2.0 Commander | Blaster Hub"
            ],
            [
                "https://instructions.hasbro.com/en-us/instruction/nerf-loadout-galactic-commander-blaster-and-48-n1-darts",
                "Nerf Loadout Galactic Commander Blaster and 48 N1 Darts Rules & Instructions - Hasbro"
            ],
            [
                "https://www.target.com/p/nerf-elite-2-0-commander-rd-6-toy-blaster/-/A-94749445",
                "NERF Elite 2.0 Commander RD 6 Toy Blaster : Target"
            ],
            [
                "https://learningposttoys.com/products/240065-nerf-elite-commander",
                "NERF ELITE COMMANDER \u2013 Learning Post & Toys"
            ],
            [
                "https://www.kroger.com/p/nerf-elite-2-0-commander-rd-6/0063050994443",
                "Nerf Elite 2.0 Commander RD-6, 1 ct - Kroger"
            ],
            [
                "https://goodbuygear.com/products/nerf-elite-2-0-commander",
                "Nerf Elite 2.0 Commander \u2014 GoodBuy Gear"
            ],
            [
                "https://www.reddit.com/r/Nerf/comments/jqfh45/new_to_nerf_should_i_buy_the_elite_20_commander/",
                "r/Nerf on Reddit: New to nerf, should i buy the elite 2.0 commander or the disruptor/strongarm? all i know is that elite 2.0 is kinda crappy"
            ],
            [
                "https://www.metromarket.net/p/nerf-elite-2-0-commander-rd-6/0063050994443",
                "Nerf Elite 2.0 Commander RD-6, 1 ct - Metro Market"
            ],
            [
                "https://www.bomgaars.com/nerf-elite-2-0-commander-rd-6-hsbe9485.html",
                "Bomgaars : Nerf Elite 2.0: Commander RD-6 : Nerf Guns"
            ],
            [
                "https://www.ralphs.com/p/nerf-elite-2-0-commander-rd-6/0063050994443",
                "Ralphs - Nerf Elite 2.0 Commander RD-6, 1 ct"
            ],
            [
                "https://www.blasterparts.com/en/p/nerf-elite-2-0-commander-rd-6--561054",
                "NERF - Elite 2.0 Commander RD-6 - blasterparts.com"
            ]
        ]
    },
    {
        "sku": "MP-000018",
        "identifier": "9780679734772",
        "item": "The House on Mango Street (ISBN, check digit valid)",
        "query": "9780679734772",
        "should_resolve": True,
        "why": "19 sources, every one of them this book. Rejected because the agreement test demands unanimity and one title lacked the shared word",
        "hits": [
            [
                "https://www.readbrightly.com/books/9780679734772/the-house-on-mango-street-by-sandra-cisneros/",
                "The House on Mango Street by Sandra Cisneros: 9780679734772 | Brightly Shop"
            ],
            [
                "https://www.amazon.com/Books-9780679734772/s?rh=n:283155,p_66:9780679734772",
                "Amazon.com: 9780679734772: Books"
            ],
            [
                "https://www.abebooks.com/9780679734772/House-Mango-Street-Sandra-Cisneros-0679734775/plp",
                "The House on Mango Street - Sandra Cisneros: 9780679734772 - AbeBooks"
            ],
            [
                "https://bulkbookstore.com/the-house-on-mango-street-9780679734772-9780679734772-1",
                "The House on Mango Street | 9780679734772 | Class Set 25+ Copies"
            ],
            [
                "https://www.ecampus.com/house-mango-street-revised-cisneros-sandra/bk/9780679734772",
                "The House on Mango Street | Rent | 9780679734772"
            ],
            [
                "https://www.chegg.com/textbooks/the-house-on-mango-street-25th-edition-9780679734772-0679734775",
                "The House on Mango Street | Buy | 9780679734772 | Chegg.com"
            ],
            [
                "https://www.walmart.com/ip/Sandra-Cisneros-The-House-on-Mango-Street-Edition-2-Paperback-9780679734772/380391",
                "Sandra Cisneros, The House on Mango Street, Edition 2, Paperback - Walmart.com"
            ],
            [
                "https://penguinrandomhousehighereducation.com/book/?isbn=9780679734772",
                "The House on Mango Street | Penguin Random House Higher Education"
            ],
            [
                "https://www.labyrinthbooks.com/the-house-on-mango-street-9780679734772/",
                "The House on Mango Street | | 9780679734772 - Labyrinth Books"
            ],
            [
                "https://dianesbooks.com/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | Diane's Books"
            ],
            [
                "https://bookpeople.com/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | BookPeople | Austin\u2019s Favorite Independent Bookstore for Books, Events & Community Since 1970"
            ],
            [
                "https://andersonsbookshop.com/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | Anderson's Bookshop Naperville"
            ],
            [
                "https://mitpressbookstore.mit.edu/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | mitpressbookstore"
            ],
            [
                "https://bookstore.holytrinityhs.org/THE-HOUSE-ON-MANGO-STREET_p_1194.html",
                "THE HOUSE ON MANGO STREET-9780679734772"
            ],
            [
                "https://palabrasbookstore.com/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | Palabras Bilingual Bookstore"
            ],
            [
                "https://storyonthesquare.com/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | Story on the Square"
            ],
            [
                "https://brooklinebooksmith.com/book/9780679734772",
                "The House on Mango Street (Vintage Contemporaries) | Brookline Booksmith"
            ],
            [
                "https://www.betterworldbooks.com/product/detail/the-house-on-mango-street-9780679734772",
                "The House on Mango Street used book by Sandra Cisneros: 9780679734772"
            ],
            [
                "https://penguinrandomhousesecondaryeducation.com/book/?isbn=9780679734772",
                "The House on Mango Street | Penguin Random House Secondary Education"
            ]
        ]
    },
    {
        "sku": "MP-000011",
        "identifier": "EOS Rebel T6i",
        "item": "Canon EOS Rebel T6i body",
        "query": "Canon EOS Rebel T6i",
        "should_resolve": True,
        "why": "six sources, all this camera. One title -- a Canon manual PDF reading 'EOS REBEL T6i (W) EOS 750D (W)' -- carries no shared word, and the unanimous intersection empties",
        "hits": [
            [
                "https://www.amazon.com/Canon-Rebel-Digital-EF-S-18-55mm/dp/B00T3ER7QO",
                "Amazon.com : Canon EOS Rebel T6i Digital SLR with EF-S 18-55mm is STM Lens - Wi-Fi Enabled : Electronics"
            ],
            [
                "https://www.reddit.com/r/Cameras/comments/18j9620/is_the_canon_eos_rebel_t6i_outdated/",
                "r/Cameras on Reddit: Is the Canon EOS Rebel T6i outdated?"
            ],
            [
                "https://www.usa.canon.com/support/p/eos-rebel-t6i",
                "Canon Support for EOS Rebel T6i | Canon U.S.A., Inc."
            ],
            [
                "https://www.dpreview.com/reviews/4471227055/canon-eos-750d-rebel-t6i/",
                "Canon EOS Rebel T6i Review | DPReview"
            ],
            [
                "https://gdlp01.c-wss.com/gds/4/0300018254/02/eos-rebelt6i-750d-im2-en.pdf",
                "EOS REBEL T6i (W) EOS 750D (W)"
            ],
            [
                "https://www.camerawholesalers.com/products/4184830574695-canon-eos-rebel-t6i-digital-slr-with-ef-s-18-135mm-is-stm-lens-wi-fi-enabled",
                "Canon EOS Rebel T6i Digital SLR with EF-S 18-135mm IS STM Lens - Wi-Fi Enabled | Camera Wholesalers"
            ]
        ]
    },
    {
        "sku": "MP-000024",
        "identifier": "A3211",
        "item": "Beats Pill speaker, brand known",
        "query": "Beats by Dr. Dre A3211",
        "should_resolve": True,
        "why": "the same code as MP-000005, disambiguated by the brand in the query",
        "hits": [
            [
                "https://shop.ezpawn.com/products/beats-by-dr-dre-a3211-beige-tan-portable-speaker",
                "Beats By Dr. Dre A3211 Beige / Tan Portable Speaker \u2013 EZPAWN"
            ],
            [
                "https://pointlomaca.paymore.com/products/beats-by-dr-dre-beats-pill-a3211-bluetooth-wireless-speaker-1771284024004-j34jl8gat",
                "Beats By Dr. Dre Beats Pill A3211 Bluetooth Wireless Speaker \u2013 PayMore Point Loma"
            ],
            [
                "https://www.pawnamerica.com/Product/Apple_A3211_Beats_Pill_Portable_Bluetooth_Speaker_Black_25523048603",
                "Apple A3211 Beats Pill Portable Bluetooth Speaker Black"
            ]
        ]
    },
    {
        "sku": "MP-000031",
        "identifier": "DS126571",
        "item": "Canon EOS Rebel T6i, regulatory model number",
        "query": "Canon DS126571",
        "should_resolve": True,
        "why": "a genuine per-product code; every hit is this camera",
        "hits": [
            [
                "https://camera.manualsonline.com/manuals/mfg/canon/ds126571.html",
                "Canon DS126571 Manual"
            ],
            [
                "https://www.pawnamerica.com/Product/Canon_DS126571_EOS_Rebel_T6i_Digital_SLR_EF_S_18_55mm_STM_Lens_Wi_Fi_Enabled_24122030636",
                "Canon DS126571 EOS Rebel T6i Digital SLR - EF-S 18-55mm STM Lens - Wi-Fi Enabled"
            ],
            [
                "https://www.gcpawn.com/search/product.php?store=002&itm_num=100878601",
                "Canon Digital SLR DS126571"
            ],
            [
                "https://picclick.com/Canon-DS126571-EOS-Rebel-T6i-242MP-401647349309.html",
                "CANON (DS126571) EOS Rebel T6i - 24.2MP - 3\" Display"
            ],
            [
                "https://shop.ezpawn.com/products/canon-ds126571-black-digital-slr-camera-2",
                "Canon Ds126571 Black Digital SLR Camera \u2013 EZPAWN"
            ],
            [
                "https://dickspawn.com/products/canon-ds126571-eos-rebel-t5i-digital-camera-with-18-55mm-lens-canon-bag",
                "Canon DS126571 EOS Rebel T5i Digital Camera with 18-55mm Lens & Canon"
            ],
            [
                "https://shop.tiktok.com/us/k/canon-ds126571",
                "canon ds126571 - TikTok Shop"
            ]
        ]
    },
    {
        "sku": "MP-000057",
        "identifier": "KM713",
        "item": "Dell KM713 wireless keyboard",
        "query": "Dell KM713",
        "should_resolve": True,
        "why": "a genuine per-product code, confirmed by the manufacturer's own site",
        "hits": [
            [
                "https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=hf7nv",
                "Dell KM713 Wireless Keyboard Caps Lock Indicator and Eject Key Application | Driver Details | Dell US"
            ],
            [
                "https://www.amazon.com/Compact-KM713-Wireless-Chrome-Keyboard/dp/B091D86PMX",
                "Amazon.com: Dell Computer KM713 Wireless Keyboard : Electronics"
            ],
            [
                "https://www.newegg.com/p/0GA-002N-00GK5",
                "Dell KM713 Wireless QWERTY102 Keys Black Keyboard 001GC - Newegg.com"
            ],
            [
                "https://www.technologygalaxy.com/Dell-KM713/p/376405",
                "Dell KM713 Wireless Chrome Keyboard KM713"
            ],
            [
                "https://www.laptopkeyboard.com/keyboards/dell/wireless-keyboard/km713/",
                "Dell Wireless Keyboard KM713 Laptop Keyboard Replacement | LaptopKeyboard.com"
            ],
            [
                "https://www.keyboardso.com/products/new-genuine-dell-km713-wireless-desktop-pc.html",
                "NEW GENUINE Dell KM713 Wireless Desktop PC US"
            ],
            [
                "https://www.manualslib.com/manual/687715/Dell-Km713.html",
                "DELL KM713 OWNER'S MANUAL Pdf Download | ManualsLib"
            ],
            [
                "https://www.laptopkeys.com/KeyboardKeys.php/Dell/Wireless%20Keyboard/KM713",
                "Dell Wireless Keyboard KM713 Laptop Keyboard Keys"
            ],
            [
                "https://dell.manymanuals.com/keyboards/km713/user-manual-8769",
                "Dell KM713 User Manual download pdf"
            ],
            [
                "https://auto.manualsonline.com/manuals/mfg/dell/km713.html?p=11",
                "Dell KM713 Manual"
            ],
            [
                "https://www.walmart.com/ip/Dell-Imsourcing-KM713-Disc-Prod-SPCL-Sourcing-See-Notes-Keyboard/378193698",
                "Dell - Imsourcing KM713 Disc Prod SPCL Sourcing See Notes Keyboard - Walmart.com"
            ]
        ]
    },
    {
        "sku": "MP-000029",
        "identifier": "TERRA",
        "item": "SodaStream Terra sparkling water maker",
        "query": "SodaStream TERRA",
        "should_resolve": True,
        "why": "fifteen sources, all this appliance",
        "hits": [
            [
                "https://sodastream.com/products/terra",
                "SodaStream Terra Sparkling Water Maker + (Quick Connect cqc bundle)"
            ],
            [
                "https://www.amazon.com/clp/B0B2X132WK",
                "Amazon.com: SodaStream TERRA Sparkling Water Maker, Starter Kit, CQC CO2 System, Includes 1 Quick Connect 60L CO2 Cylinder, 1 Dishwasher Safe BPA Free Bottle, 1 bubly Lime Drop, Make your own Pepsi, Black: Home & Kitchen"
            ],
            [
                "https://www.target.com/p/sodastream-terra-sparkling-water-maker/-/A-84691909?showOnlyQuestions=true",
                "SodaStream Terra Sparkling Water Maker with CO2 and Carbonating Bottle : Target"
            ],
            [
                "https://www.reddit.com/r/SodaStream/comments/1frr0ul/why_i_got_a_soda_stream_terra/",
                "r/SodaStream on Reddit: Why I got a soda stream terra"
            ],
            [
                "https://www.youtube.com/watch?v=Adv1lG6FTrg",
                "SodaStream Terra Tutorial | How To Use It Properly - YouTube"
            ],
            [
                "https://www.kohls.com/product/prd-5176342/sodastream-terra-sparkling-water-maker-starter-kit.jsp",
                "SodaStream Terra Sparkling Water Maker Starter Kit"
            ],
            [
                "https://www.realhomes.com/reviews/sodastream-terra-review",
                "Sodastream Terra: This sparkling water maker can make fizzy favorites (including Kombucha) for less"
            ],
            [
                "https://www.ipromo.com/sodastream-terra.html",
                "Buy SodaStream Terra | iPromo"
            ],
            [
                "https://www.walmart.com/ip/SodaStream-Terra-Black-Sparkling-Water-Maker-with-CO2-and-Carbonating-Bottle/753883143",
                "SodaStream Terra Sparkling Water Maker with CO2 and Carbonating Bottle - Walmart.com"
            ],
            [
                "https://www.cvs.com/shop/sodastream-terra-sparkling-beverage-maker-black-prodid-383407",
                "SodaStream Terra Sparkling Beverage Maker, Black - CVS Pharmacy"
            ],
            [
                "https://www.cnet.com/home/kitchen-and-household/sodastream-terra-review/",
                "A new $100 SodaStream model has one noticeable improvement - CNET"
            ],
            [
                "https://www.costco.com/p/-/sodastream-terra-cqc-bundle/4000153599",
                "Sodastream Terra CQC Bundle | Costco"
            ],
            [
                "https://whisknyc.com/products/white-sodastream-terra-in-store-pick-up-only",
                "sodastream terra, white"
            ],
            [
                "https://www.frysfood.com/p/sodastream-terra-sparkling-water-maker/0081855802801",
                "SodaStream\u00ae Terra Sparkling Water Maker, 1 ct - Fry\u2019s Food Stores"
            ],
            [
                "https://www.bestbuy.com/product/sodastream-terra-water-maker-kit-red/J3YCZL6KZG/sku/6474540/reviews",
                "SodaStream Terra Water Maker Kit Red 1012811012"
            ]
        ]
    },
    {
        "sku": "MP-000046",
        "identifier": "DJI Osmo Action",
        "item": "DJI Osmo Action camera",
        "query": "DJI Osmo Action",
        "should_resolve": False,
        "why": "correctly unresolved today: the hits are Action 4, Action 5 Pro and a shop page, which are different products",
        "hits": [
            [
                "https://www.dji.com/osmo-action-4",
                "DJI Osmo Action 4 - Set the Tone - DJI United States"
            ],
            [
                "https://www.amazon.com/clp/B07RJMK2GV",
                "Amazon.com : DJI Osmo Action 5 Pro Standard Combo, Waterproof Action Camera with 1/1.3\" Sensor, 4K/120fps Video, Subject Tracking, Stabilization, Dual OLED Touchscreens, Ideal for Sports, Vlog : Electronics"
            ],
            [
                "https://www.reddit.com/r/djiosmo/comments/1o74kzc/after_buying_the_osmo_action_5_pro_im_starting_to/",
                "r/djiosmo on Reddit: After buying the Osmo Action 5 Pro, I\u2019m starting to doubt my choice\u2026"
            ],
            [
                "https://www.dronenerds.com/collections/cameras-sensors-consumer-cameras-osmo-action",
                "\ud83c\udff7\ufe0f Shop All DJI Osmo Action | at Drone Nerds"
            ],
            [
                "https://www.adorama.com/djiosmocam.html",
                "DJI Osmo Action 4K HDR Camera"
            ],
            [
                "https://www.bestbuy.com/product/dji-osmo-action-4-4k-action-camera-standard-bundle-gray/JJ82L2VK8Z/sku/6546636",
                "DJI Osmo Action 4 4K Action Camera Standard Bundle Gray CP.OS.00000269.01 - Best Buy"
            ],
            [
                "https://www.huntsphotoandvideo.com/detail_page.cfm?productid=CPOS0000027001",
                "Stabilizing Rigs: DJI Osmo Action 4 Adventure Combo Product Information"
            ]
        ]
    },
    {
        "sku": "MP-000049",
        "identifier": "17070",
        "item": "iRobot Roomba dock charger",
        "query": "iRobot 17070",
        "should_resolve": False,
        "why": "correctly unresolved today: a manual, a third-party charger and a refurbished dock",
        "hits": [
            [
                "https://www.manualslib.com/products/Irobot-17070-13827588.html",
                "Irobot 17070 Manuals | ManualsLib"
            ],
            [
                "https://www.amazon.com/Charger-charging-ADF-N1-17064-4452369/dp/B0DHNXHYVK",
                "Amazon.com: for Roomba Charger, for Roomba Charger Dock, Charging Base with Tools | Docking Station for Roomba e5 e6 i1 i3 i4 i6 i7 i8 500 600 700 800 900 Robots ADF-N1 17170 17064 4452369 : Tools & Home Improvement"
            ],
            [
                "https://www.newegg.com/irobot-17070/p/13B-04WW-00005",
                "Refurbished: iRobot Roomba Dock Charger Model 17070 w/ Cord - Newegg.com"
            ]
        ]
    },
    {
        "sku": "MP-000052",
        "identifier": "Air Jordan 1 High",
        "item": "Nike Air Jordan 1 High",
        "query": "Nike Air Jordan 1 High",
        "should_resolve": False,
        "why": "correctly unresolved today: one source only",
        "hits": [
            [
                "https://www.footlocker.com/buy/air-jordan-1-high-og-sneakers-0bez00a",
                "Air Jordan 1 High OG Sneakers | Foot Locker"
            ]
        ]
    }
]


def case(sku: str) -> dict:
    """One replay case by the SKU it was captured from."""
    for entry in CASES:
        if entry["sku"] == sku:
            return entry
    raise KeyError(f"no replay case for {sku}")
