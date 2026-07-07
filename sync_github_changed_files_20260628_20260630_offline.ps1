param(
    [string]$SourceRoot = "C:\Users\Administrator\Desktop\Workflow-PNCECS",
    [string]$DestinationRoot = "C:\Apps\Workflow_PNCECS",
    [ValidateSet("Copy", "Move")]
    [string]$Mode = "Copy",
    [switch]$Execute,
    [switch]$SkipMissing,
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"

# Offline GitHub snapshot: files changed on origin/main from 2026-06-28 00:00:00 through 2026-06-30 23:59:59.
# This script does not call git and does not require internet access. It only copies/moves these paths from SourceRoot to DestinationRoot.
$ChangedFiles = @(
    "admin/evaluations.py",
    "admin/masterdata.py",
    "admin/routes.py",
    "audit/routes.py",
    "instance/tmp/workflow_backup_20260628_055635_46c0eb50e8674e909407300b7443a4a0/workflow_backup_20260628_055635.zip",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_2_2371c5490bed4690aa1a3a3403957898.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_3_4d2f77439d49474e98f805deae1475e8.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_3_85c24e2205a54922b3e1199748f8ef34.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_4_272ba3e6e4bf4b74b35388d678ba8575.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_4_b84f42125e8d4a6bb32da04641eaffe0.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_5_21d908a15dc7410684dd83c5815f0b7e.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_5_9d3ca157332044eeba83d6b3123fb0bc.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_6_9dbbbd4763cf4857b923c6a23163d219.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/IN_7_26412c7a29be45da9a31f7a77de8072a.jpeg",
    "instance/uploads_before_restore_20260629_063322/correspondence/OUT_1_6695e9d92f8745228a7522a78adf5d89.pdf",
    "instance/uploads_before_restore_20260629_063322/correspondence/OUT_2_2c61b7c26ce84860920def475c92a86b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/1/2edc340c92784d4c99193d7bcf11eb55.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/1/3bc7530b01aa4fb3a317d09b20efe32b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/1/a4be0b4494ed4ba7a5608cc36e99c79f.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/1/afc1d8e9a01e42c2bd731cc6fb0bfa51.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/1/c443c037b78c452eb1999315b9d54ff9.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/10/0c2f83adb44345ffa2514baaaa726d76.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/10/56b8a0699b2a46e4bc1a17694d2a55c2.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/10/aec214b044c4451e8ffb22defd0362bd.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/10/dcebe0481c4a4c9a8b5f95f49fde7576.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/10/e11bb07178ba4df79734a5755890be6d.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/11/3124c89ded844e739258034ce408da40.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/11/39f1a531ed8e431180edf265b0cf4216.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/11/3c89424e0a0642a2bda9116908bfa90a.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/11/98c277b6260d4254b30862dbaaab8cf7.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/11/bb7011b3ed3444cdaf277577d6b437d6.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/12/17203472e13345bbb4357708b2f35f7b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/12/2dc3b393a0324e16a3304b05bc780bf6.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/12/6297a43f88844968b3de05f4560532d9.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/12/69b54ed46b404d298cec357aaaf9d886.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/12/9d9b9190977440b493739e36dfba3cab.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/13/14c88d336a0d473c914eac3cbf099eb8.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/13/3e933541724642b7beb4645d8d859639.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/13/7e3bdcb01a9a4bb087d3eb422cc4523d.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/13/8f400b7ddeec47128637f8acf1ef8df5.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/13/f451f59f4eed4edf91cc90b7832bae24.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/14/029e5bf8581249278f1bfd6f36876b7c.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/14/0a8a8d2fd2e84a9e96df02dccdea0a78.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/14/4d48e8e3fc2c430c8059ca932e4e6ce6.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/14/5cc6cf8a698e474db6bbf63755e2ccab.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/14/afd42c6e7a084a7ab0ee72d172d416dc.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/17/180311feaa4d484e856a94d875587e81.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/17/2cb80182607c4c5babc08f09546af1bc.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/17/7d37fd47b2f5471e9797460b61893bd4.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/17/ccce79a4090e4fb3b064d9f1b2ce08af.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/17/e9c9da3b0c2f4d2a9f3a77260b90a05e.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/18/10b833a742324cbbb4f0aaf58a3da969.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/18/35c6b6dcf5054c63b84c51a69beae18c.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/18/6528a212da05498fb0ddbcdb4b69f6e9.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/18/9d06389a37d041c7b8226fb2711b1a60.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/18/e31ec121994b4d8d8a4be8f136e6dcc3.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/19/318098f0163c434ba411620978e19c0a.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/19/6830c8e5863a4fa1a2a159862844bd28.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/19/a2db91f5b45041c8ba27548f2f3ae2a4.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/19/b8ff31c010594d37a4fdfd163547b5ee.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/19/ddb40efc406441dcaac28c7da47897b6.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/2/0f4bad09e9ce4e2aa75d21681a702db0.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/2/3dc46ff4ec594aa88f7d85e2555fbd9c.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/2/41b5e20e32d7470a978df96e50839b48.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/2/b08ec691a3534ab7af4097a2a8369012.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/2/db4aa1b1a0e341a5a6c01edc59108779.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/20/121487eeee6c42d6a564fa8ae3b09a4b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/20/142f8047934e4b6b8ce0ad5740533e26.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/20/4e670353794f4f01a487b9d7e60dfafc.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/20/61da071dbdd744bd800c10de18f9cb78.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/20/c0729026fedd444a84b5b98a4368e208.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/21/6641d60c3cd14865b2ce19fc25fdf368.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/21/6cdee35ee08b42a885554de23f60115a.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/21/beeb7e7dc44a4d118995f5888a7e08e0.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/21/c3466c06a1274395a1276977f85eb1aa.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/21/d85f21750d864502b4e728a7b0a46555.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/22/11d97ec6c5cc400db23bf1407e93c891.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/22/3ce30fde96814850a8e5bd28a340c1a7.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/22/49ef74e334744ebcbc5f9b9970cfdfa8.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/22/6d4eda1902354ffeaf78492fbe8a803f.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/22/cabc0c8b469d4eda9f8d0f3065f48a0b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/3/2016b32b8d4942c6985d12d925a9ee64.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/3/2039b5ca96b5483187eda8f36e7d7e62.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/3/6291f4c141214b568e27d6ed0230d398.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/3/9935215bda1a4b61b97f36a3cb22ee33.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/3/ebfb75c5f24946608bb7af3c55108e08.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/30/03fef037d8944ac2ad37fe0832040d2e.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/30/0f1944b274cf40a9b9b1a837d89bfb18.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/30/7be2c1b639ba40378e314b11e42172a4.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/30/7e9489522b364d8e87a5b228de306caa.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/30/c8e4d06a87b047d68d48042aeb4de262.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/33/1c0c5dac714344418deb75bd52ce65df.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/33/560db6fdda2c465587318ae898d6f887.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/33/81bd6e50476f4566989e52b85b21be80.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/33/af6c666f04b344ba9bd9e02f992021fb.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/33/fde1d4e1218b44038879950bbec50cbb.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/36/01ffcf761df34909adefc45646a09267.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/36/060b71c6450f4cb9a9a1f2a2b1949d98.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/36/68b9a61581b14b6ea96dcd1e200aca99.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/36/93cf2f518c85458e80980329ebe0832b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/36/b9bfd2e34e544860aa9a54cc97c17719.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/39/376ce5bbec114961a2d26d078b53f6b6.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/39/726c99fecbc442cba8b06e93d34a9ec2.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/39/d0334eb0d4474701af5b57fc87055a18.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/39/e2a632d2a8124a459adc38b4cee76d43.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/39/e5cbe38cae7a4c19bf8aca95a7139b89.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/4/33ea79d19988479bad192c5327ea0c4d.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/4/37a51c73f137419a8acecb5a985e8950.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/4/3f8fae995f40410c890579f6ac6d2fd8.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/4/a85b1f16144a48069b8e92d9ac828311.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/4/e1b3bb1d7d134c20b357a8b3658fa912.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/40/3ea62662fe2d43a98c460998e9c4954b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/40/70fe2b87c52e41769b7f86232f3674cc.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/40/a77e59c2be6f420c823439e5e4156133.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/40/b611f1499a8f49b58f86647eef0b045c.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/40/c03b34712ddc4e84948ca63bf581bc5e.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/43/2154977bb27a4671a6eb5276fcbaeef8.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/43/3e11863df9bc444b8e96edc10be33715.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/43/4a839cdf70c24c548d29faeaabdc70ad.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/43/63c20c41f3fc44b2b68251007654e1d2.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/43/93af9900109243fa90795cdffe087347.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/45/5acdf89bcebe4c95af9390730d67e318.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/45/685dca09dca840b088198521d9d41068.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/45/6900fba8e656467b9317b94722a8b454.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/45/69d778fc85dc4c7c929d218a30cef9ab.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/45/d4adf065ef934e78bcbbf778e4bb8993.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/46/1a82e1446a5b4957b9255593d086b251.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/46/4641371fe528408ca2135edfbdd0bfbb.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/46/4812150543ce46168f4ad476b373821a.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/46/c5864acd91924d1189305fd5ee326707.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/46/e785d6f60f2d4d69b08860ea6ec3730b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/47/1012221351424a6383e965bf960c2b2a.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/47/693772de47f2427097a0fe345040182f.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/47/e34cc7d4ac8e483f948d86385d9cee5b.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/47/e7f56026f204414e9b080131bdc0df48.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/47/fb41ba4825a146f49a5ea2f3f2e1393f.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/49/074766e26f624cdcb66fc7f5e733b5ae.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/49/282dee333a734d04a9088131507c1429.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/49/42400c46aebd4fa7935ce5e238c7c9d2.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/49/da84b01a0ffb4f1a83c8641f82719bda.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/49/e13fcd52e9fc4722accc390878ae0cea.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/5/888c3811505c42b787151a27775bca4d.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/5/b6e1675fed1d4c91b7dc9b2f85ce7fa2.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/50/0952dc9808004c8d8e10cbe3dbb64da0.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/50/3f266f667c564f60b5169bb70e3ea409.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/50/e51adfe34cd14f49814a5f581f27bbba.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/50/ead42b477bb44f2bbfe68e1a3e2fa3c9.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/50/ed160aaebeea411f8b04f0b8c8f0e04e.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/6/0df8933d2ec1442ba43b36a72d5313fa.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/6/76b8b0d380c94a319bf59bfafe61e6b6.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/6/774b89c0e09b4c65a963bb4943743fd2.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/6/a307b490f4c840c8a9979acf79c7a1bd.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/6/b2e310fe9e8641b2aa16d878571fb619.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/7/0a70a8c525d04a0881624a0f631b8f7f.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/7/c22a05361b5e43709530618712d1f557.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/7/cd9b8578e7724c23a9d0fb9fbf52a6de.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/7/dfc84687a36d4da4a69f23d81cc91ee0.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/7/e0ea9f3f8472446aa308d5fcf64494cc.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/8/24debf57c6eb4dfe8d5e7c71753d91a0.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/8/7abd9592ffe24a70aee240b12943665e.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/8/9a873441632348feb725c283bfc36a1f.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/8/b70f4064b540474fadb7cf22f399f631.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/8/c4acc99197804929b507ff9d63df6145.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/9/267413ef33e94628b411119a6093fa8d.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/9/65709face5c045eea324e113b41f2b90.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/9/7987d4dc806b41598ed7d68af50b3ce0.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/9/822a2b9d0e114def8dba9dfa6f207221.pdf",
    "instance/uploads_before_restore_20260629_063322/employees/9/c618de4249d144e2b942ad74661f24b9.pdf",
    "instance/uploads_before_restore_20260629_063322/hr_docs/1/75e9102a61da40b6a82d04285719181f.pdf",
    "instance/uploads_before_restore_20260629_063322/hr_docs/2/0f17626ceaee4fffaa128668c5291709.docx",
    "instance/uploads_before_restore_20260629_063322/hr_docs/3/7de8ded0c1e843b09d40b2b97ca06446.docx",
    "instance/uploads_before_restore_20260629_063322/hr_docs/4/2710bf6e0ced42d18fd9b0b9d3cc22c6.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/input/96_102025_WB.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/404112773.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/404112773_2.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/410232326.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/410588552.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/411026065.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/411862964.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/412207292.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/412578742.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/850403759.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/850686262.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/853274702.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/853402204.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/854272796.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/854554516.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_2.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_3.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_4.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_5.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_6.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_7.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/900330895_8.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/904076957.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/904385895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/906452610.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/906834163.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/907026728.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/907878177.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/910668193.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/911606564.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/918780032.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/918780032_2.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/918780032_3.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/918780032_4.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/918780032_5.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/918780032_6.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/921587440.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/948497227.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/950903765.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/968483966.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/982776676.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/984367268.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/999459555.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/output/999828031.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_10_20260624_112240_9b38b1c4/payslips_split.zip",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/input/96_112025_WB.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/404112773.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/410232326.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/410588552.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/411026065.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/411862964.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/412207292.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/412578742.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/850403759.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/850686262.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/853274702.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/853402204.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/854272796.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/854554516.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/900330895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/904076957.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/904385895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/906452610.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/906834163.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/907026728.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/907878177.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/910668193.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/911606564.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/918780032.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/921587440.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/948497227.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/950903765.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/968483966.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/982776676.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/984367268.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/999459555.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/output/999828031.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_11_20260624_112206_4763289a/payslips_split.zip",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/input/96_122025_WB.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/404112773.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/410232326.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/410588552.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/411026065.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/411862964.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/412207292.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/412578742.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/850403759.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/850686262.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/853274702.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/853402204.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/854272796.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/854554516.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/900330895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957_2.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957_3.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957_4.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957_5.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957_6.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904076957_7.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/904385895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/906452610.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/906834163.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/907026728.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/907878177.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/910668193.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/911606564.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/918780032.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/921587440.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/948497227.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/950903765.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/968483966.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/982776676.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/984367268.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/999459555.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/output/999828031.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2025_12_20260624_112019_259c7096/payslips_split.zip",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/input/96_12026_WB.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/404112773.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/410232326.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/410588552.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/411026065.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/411862964.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/412207292.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/412578742.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/850403759.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/850686262.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/853274702.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/853402204.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/854272796.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/854554516.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/900330895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/904076957.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/904385895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/906452610.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/906834163.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/907026728.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/907878177.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/910668193.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/911606564.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/918780032.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/921587440.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/948497227.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/950903765.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/968483966.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/979941168.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/982776676.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/984367268.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/999459555.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/output/999828031.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_01_20260624_112104_701b47c1/payslips_split.zip",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/input/96_22026_WB.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/404112773.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/410232326.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/410588552.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/411026065.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/411862964.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/412207292.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/412578742.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/850403759.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/850686262.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/853274702.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/853402204.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/854272796.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/854554516.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/900330895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/904076957.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/904385895.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/906452610.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/906834163.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/907026728.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/907878177.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/910668193.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/911606564.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/918780032.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/921587440.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/948497227.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/950903765.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/968483966.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/979941168.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/982776676.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/984367268.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/999459555.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/output/999828031.pdf",
    "instance/uploads_before_restore_20260629_063322/payslips/2026_02_20260617_102848_33fdee20/payslips_split.zip",
    "messages/routes.py",
    "portal/perm_defs.py",
    "portal/routes.py",
    "services/evaluation_service.py",
    "static/uploads_before_restore_20260629_063322/avatars/user_6_1773051837.jpg",
    "sync_changed_files_20260624_20260625.ps1",
    "sync_github_changed_files_20260626_20260628.ps1",
    "sync_github_changed_files_20260627_20260629.ps1",
    "templates/admin/evaluation_view.html",
    "templates/admin/evaluations.html",
    "templates/messages/compose.html",
    "templates/messages/sent.html",
    "templates/partials/evaluation_breakdown.html",
    "templates/partials/sidebar_content.html",
    "templates/portal/admin/integrations.html",
    "templates/portal/hr/attendance_batches.html",
    "templates/portal/hr/attendance_import.html",
    "templates/portal/hr/my_system_evaluation_view.html",
    "templates/portal/layout.html",
    "templates/portal/meetings/minutes_preview.html",
    "templates/portal/meetings/view.html",
    "workflow/routes.py"
)

function Resolve-ExistingDirectory {
    param([string]$Path, [string]$Name)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Name does not exist or is not a directory: $Path"
    }

    return (Resolve-Path -LiteralPath $Path).Path.TrimEnd("\")
}

function Get-NormalizedRelativePath {
    param([string]$RelativePath)

    return ($RelativePath -replace "/", "\").TrimStart("\")
}

$source = Resolve-ExistingDirectory -Path $SourceRoot -Name "SourceRoot"
$destination = $DestinationRoot.TrimEnd("\")

if ($source -ieq $destination) {
    throw "SourceRoot and DestinationRoot are the same path."
}

if (-not $ReportPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $ReportPath = Join-Path (Get-Location).Path "github_changed_files_20260628_20260630_offline_$stamp.csv"
}

$files = foreach ($relativePath in $ChangedFiles) {
    $relative = Get-NormalizedRelativePath -RelativePath $relativePath
    $sourcePath = Join-Path $source $relative
    $targetPath = Join-Path $destination $relative
    $exists = Test-Path -LiteralPath $sourcePath -PathType Leaf
    $sourceItem = if ($exists) { Get-Item -LiteralPath $sourcePath } else { $null }

    [pscustomobject]@{
        Exists        = $exists
        LastWriteTime = if ($sourceItem) { $sourceItem.LastWriteTime } else { $null }
        SizeBytes     = if ($sourceItem) { $sourceItem.Length } else { $null }
        RelativePath  = $relative
        SourcePath    = $sourcePath
        TargetPath    = $targetPath
    }
}

$files | Export-Csv -LiteralPath $ReportPath -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "GitHub changed files list: 2026-06-28 to 2026-06-30 (offline snapshot)"
Write-Host "Source:      $source"
Write-Host "Destination: $destination"
Write-Host "Mode:        $Mode"
Write-Host "Execute:     $($Execute.IsPresent)"
Write-Host "SkipMissing: $($SkipMissing.IsPresent)"
Write-Host "Report:      $ReportPath"
Write-Host "Count:       $($files.Count)"
Write-Host ""

$files | Format-Table Exists, LastWriteTime, SizeBytes, RelativePath -AutoSize

$missingFiles = @($files | Where-Object { -not $_.Exists })
if ($missingFiles.Count -gt 0) {
    Write-Host ""
    Write-Warning "Missing source files: $($missingFiles.Count)"
    $missingFiles | ForEach-Object { Write-Warning $_.SourcePath }

    if (-not $SkipMissing) {
        Write-Host ""
        Write-Host "Add -SkipMissing to continue without missing files."
        exit 1
    }
}

if (-not $Execute) {
    Write-Host ""
    Write-Host "Dry run only. To transfer files, rerun with -Execute."
    Write-Host "Copy files: .\sync_github_changed_files_20260628_20260630_offline.ps1 -Execute -Mode Copy"
    Write-Host "Move files: .\sync_github_changed_files_20260628_20260630_offline.ps1 -Execute -Mode Move"
    exit 0
}

$transferred = 0
foreach ($file in ($files | Where-Object { $_.Exists })) {
    $targetDir = Split-Path -Parent $file.TargetPath
    if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    if ($Mode -eq "Move") {
        Move-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    } else {
        Copy-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    }

    $transferred += 1
}

Write-Host ""
Write-Host "$Mode completed for $transferred files."
