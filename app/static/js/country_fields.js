(function () {
  var COUNTRIES = [
    {name:"Maldives",flag:"🇲🇻",code:"+960"},
    {name:"Afghanistan",flag:"🇦🇫",code:"+93"},
    {name:"Albania",flag:"🇦🇱",code:"+355"},
    {name:"Algeria",flag:"🇩🇿",code:"+213"},
    {name:"Andorra",flag:"🇦🇩",code:"+376"},
    {name:"Angola",flag:"🇦🇴",code:"+244"},
    {name:"Argentina",flag:"🇦🇷",code:"+54"},
    {name:"Armenia",flag:"🇦🇲",code:"+374"},
    {name:"Australia",flag:"🇦🇺",code:"+61"},
    {name:"Austria",flag:"🇦🇹",code:"+43"},
    {name:"Azerbaijan",flag:"🇦🇿",code:"+994"},
    {name:"Bahrain",flag:"🇧🇭",code:"+973"},
    {name:"Bangladesh",flag:"🇧🇩",code:"+880"},
    {name:"Belarus",flag:"🇧🇾",code:"+375"},
    {name:"Belgium",flag:"🇧🇪",code:"+32"},
    {name:"Bhutan",flag:"🇧🇹",code:"+975"},
    {name:"Bolivia",flag:"🇧🇴",code:"+591"},
    {name:"Bosnia and Herzegovina",flag:"🇧🇦",code:"+387"},
    {name:"Botswana",flag:"🇧🇼",code:"+267"},
    {name:"Brazil",flag:"🇧🇷",code:"+55"},
    {name:"Brunei",flag:"🇧🇳",code:"+673"},
    {name:"Bulgaria",flag:"🇧🇬",code:"+359"},
    {name:"Cambodia",flag:"🇰🇭",code:"+855"},
    {name:"Cameroon",flag:"🇨🇲",code:"+237"},
    {name:"Canada",flag:"🇨🇦",code:"+1"},
    {name:"Chile",flag:"🇨🇱",code:"+56"},
    {name:"China",flag:"🇨🇳",code:"+86"},
    {name:"Colombia",flag:"🇨🇴",code:"+57"},
    {name:"Costa Rica",flag:"🇨🇷",code:"+506"},
    {name:"Croatia",flag:"🇭🇷",code:"+385"},
    {name:"Cuba",flag:"🇨🇺",code:"+53"},
    {name:"Cyprus",flag:"🇨🇾",code:"+357"},
    {name:"Czech Republic",flag:"🇨🇿",code:"+420"},
    {name:"Denmark",flag:"🇩🇰",code:"+45"},
    {name:"Dominican Republic",flag:"🇩🇴",code:"+1-809"},
    {name:"DR Congo",flag:"🇨🇩",code:"+243"},
    {name:"Ecuador",flag:"🇪🇨",code:"+593"},
    {name:"Egypt",flag:"🇪🇬",code:"+20"},
    {name:"El Salvador",flag:"🇸🇻",code:"+503"},
    {name:"Eritrea",flag:"🇪🇷",code:"+291"},
    {name:"Estonia",flag:"🇪🇪",code:"+372"},
    {name:"Ethiopia",flag:"🇪🇹",code:"+251"},
    {name:"Fiji",flag:"🇫🇯",code:"+679"},
    {name:"Finland",flag:"🇫🇮",code:"+358"},
    {name:"France",flag:"🇫🇷",code:"+33"},
    {name:"Georgia",flag:"🇬🇪",code:"+995"},
    {name:"Germany",flag:"🇩🇪",code:"+49"},
    {name:"Ghana",flag:"🇬🇭",code:"+233"},
    {name:"Greece",flag:"🇬🇷",code:"+30"},
    {name:"Guatemala",flag:"🇬🇹",code:"+502"},
    {name:"Guinea",flag:"🇬🇳",code:"+224"},
    {name:"Haiti",flag:"🇭🇹",code:"+509"},
    {name:"Honduras",flag:"🇭🇳",code:"+504"},
    {name:"Hungary",flag:"🇭🇺",code:"+36"},
    {name:"Iceland",flag:"🇮🇸",code:"+354"},
    {name:"India",flag:"🇮🇳",code:"+91"},
    {name:"Indonesia",flag:"🇮🇩",code:"+62"},
    {name:"Iran",flag:"🇮🇷",code:"+98"},
    {name:"Iraq",flag:"🇮🇶",code:"+964"},
    {name:"Ireland",flag:"🇮🇪",code:"+353"},
    {name:"Israel",flag:"🇮🇱",code:"+972"},
    {name:"Italy",flag:"🇮🇹",code:"+39"},
    {name:"Jamaica",flag:"🇯🇲",code:"+1-876"},
    {name:"Japan",flag:"🇯🇵",code:"+81"},
    {name:"Jordan",flag:"🇯🇴",code:"+962"},
    {name:"Kazakhstan",flag:"🇰🇿",code:"+7"},
    {name:"Kenya",flag:"🇰🇪",code:"+254"},
    {name:"Kuwait",flag:"🇰🇼",code:"+965"},
    {name:"Kyrgyzstan",flag:"🇰🇬",code:"+996"},
    {name:"Laos",flag:"🇱🇦",code:"+856"},
    {name:"Latvia",flag:"🇱🇻",code:"+371"},
    {name:"Lebanon",flag:"🇱🇧",code:"+961"},
    {name:"Libya",flag:"🇱🇾",code:"+218"},
    {name:"Lithuania",flag:"🇱🇹",code:"+370"},
    {name:"Luxembourg",flag:"🇱🇺",code:"+352"},
    {name:"Madagascar",flag:"🇲🇬",code:"+261"},
    {name:"Malawi",flag:"🇲🇼",code:"+265"},
    {name:"Malaysia",flag:"🇲🇾",code:"+60"},
    {name:"Mali",flag:"🇲🇱",code:"+223"},
    {name:"Malta",flag:"🇲🇹",code:"+356"},
    {name:"Mauritius",flag:"🇲🇺",code:"+230"},
    {name:"Mexico",flag:"🇲🇽",code:"+52"},
    {name:"Moldova",flag:"🇲🇩",code:"+373"},
    {name:"Mongolia",flag:"🇲🇳",code:"+976"},
    {name:"Montenegro",flag:"🇲🇪",code:"+382"},
    {name:"Morocco",flag:"🇲🇦",code:"+212"},
    {name:"Mozambique",flag:"🇲🇿",code:"+258"},
    {name:"Myanmar",flag:"🇲🇲",code:"+95"},
    {name:"Namibia",flag:"🇳🇦",code:"+264"},
    {name:"Nepal",flag:"🇳🇵",code:"+977"},
    {name:"Netherlands",flag:"🇳🇱",code:"+31"},
    {name:"New Zealand",flag:"🇳🇿",code:"+64"},
    {name:"Nicaragua",flag:"🇳🇮",code:"+505"},
    {name:"Nigeria",flag:"🇳🇬",code:"+234"},
    {name:"Norway",flag:"🇳🇴",code:"+47"},
    {name:"Oman",flag:"🇴🇲",code:"+968"},
    {name:"Pakistan",flag:"🇵🇰",code:"+92"},
    {name:"Palestine",flag:"🇵🇸",code:"+970"},
    {name:"Panama",flag:"🇵🇦",code:"+507"},
    {name:"Paraguay",flag:"🇵🇾",code:"+595"},
    {name:"Peru",flag:"🇵🇪",code:"+51"},
    {name:"Philippines",flag:"🇵🇭",code:"+63"},
    {name:"Poland",flag:"🇵🇱",code:"+48"},
    {name:"Portugal",flag:"🇵🇹",code:"+351"},
    {name:"Qatar",flag:"🇶🇦",code:"+974"},
    {name:"Romania",flag:"🇷🇴",code:"+40"},
    {name:"Russia",flag:"🇷🇺",code:"+7"},
    {name:"Rwanda",flag:"🇷🇼",code:"+250"},
    {name:"Saudi Arabia",flag:"🇸🇦",code:"+966"},
    {name:"Senegal",flag:"🇸🇳",code:"+221"},
    {name:"Serbia",flag:"🇷🇸",code:"+381"},
    {name:"Singapore",flag:"🇸🇬",code:"+65"},
    {name:"Slovakia",flag:"🇸🇰",code:"+421"},
    {name:"Slovenia",flag:"🇸🇮",code:"+386"},
    {name:"Somalia",flag:"🇸🇴",code:"+252"},
    {name:"South Africa",flag:"🇿🇦",code:"+27"},
    {name:"South Korea",flag:"🇰🇷",code:"+82"},
    {name:"South Sudan",flag:"🇸🇸",code:"+211"},
    {name:"Spain",flag:"🇪🇸",code:"+34"},
    {name:"Sri Lanka",flag:"🇱🇰",code:"+94"},
    {name:"Sudan",flag:"🇸🇩",code:"+249"},
    {name:"Sweden",flag:"🇸🇪",code:"+46"},
    {name:"Switzerland",flag:"🇨🇭",code:"+41"},
    {name:"Syria",flag:"🇸🇾",code:"+963"},
    {name:"Taiwan",flag:"🇹🇼",code:"+886"},
    {name:"Tajikistan",flag:"🇹🇯",code:"+992"},
    {name:"Tanzania",flag:"🇹🇿",code:"+255"},
    {name:"Thailand",flag:"🇹🇭",code:"+66"},
    {name:"Togo",flag:"🇹🇬",code:"+228"},
    {name:"Trinidad and Tobago",flag:"🇹🇹",code:"+1-868"},
    {name:"Tunisia",flag:"🇹🇳",code:"+216"},
    {name:"Turkey",flag:"🇹🇷",code:"+90"},
    {name:"Turkmenistan",flag:"🇹🇲",code:"+993"},
    {name:"Uganda",flag:"🇺🇬",code:"+256"},
    {name:"Ukraine",flag:"🇺🇦",code:"+380"},
    {name:"United Arab Emirates",flag:"🇦🇪",code:"+971"},
    {name:"United Kingdom",flag:"🇬🇧",code:"+44"},
    {name:"United States",flag:"🇺🇸",code:"+1"},
    {name:"Uruguay",flag:"🇺🇾",code:"+598"},
    {name:"Uzbekistan",flag:"🇺🇿",code:"+998"},
    {name:"Venezuela",flag:"🇻🇪",code:"+58"},
    {name:"Vietnam",flag:"🇻🇳",code:"+84"},
    {name:"Yemen",flag:"🇾🇪",code:"+967"},
    {name:"Zambia",flag:"🇿🇲",code:"+260"},
    {name:"Zimbabwe",flag:"🇿🇼",code:"+263"}
  ];

  // Sort: Maldives first, then alphabetical
  var sorted = COUNTRIES.slice().sort(function(a, b) {
    if (a.name === 'Maldives') return -1;
    if (b.name === 'Maldives') return 1;
    return a.name.localeCompare(b.name);
  });

  window.initPhoneField = function(dialId, localId, hiddenId, existingValue) {
    var dialSel  = document.getElementById(dialId);
    var localInp = document.getElementById(localId);
    var hidden   = document.getElementById(hiddenId);
    if (!dialSel || !localInp || !hidden) return;

    sorted.forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c.code;
      opt.textContent = c.flag + ' ' + c.name + ' ' + c.code;
      if (c.name === 'Maldives') opt.selected = true;
      dialSel.appendChild(opt);
    });

    if (existingValue) {
      var num = existingValue.charAt(0) === '+' ? existingValue : ('+' + existingValue);
      // Try longest code first to avoid "+1" matching before "+1-876"
      var byLen = COUNTRIES.slice().sort(function(a,b){ return b.code.length - a.code.length; });
      var matched = false;
      for (var i = 0; i < byLen.length; i++) {
        if (num.indexOf(byLen[i].code) === 0) {
          dialSel.value = byLen[i].code;
          localInp.value = num.slice(byLen[i].code.length);
          hidden.value = num;
          matched = true;
          break;
        }
      }
      if (!matched) {
        localInp.value = existingValue.replace(/^\+/, '');
        hidden.value = existingValue;
      }
    } else {
      hidden.value = '+960';
    }

    function updateHidden() {
      var local = localInp.value.replace(/\D/g, '');
      hidden.value = local ? (dialSel.value + local) : '';
    }
    dialSel.addEventListener('change', updateHidden);
    localInp.addEventListener('input', updateHidden);
  };

  window.initNationalityField = function(inputId, datalistId, existingValue) {
    var datalist = document.getElementById(datalistId);
    var input    = document.getElementById(inputId);
    if (!datalist || !input) return;

    sorted.forEach(function(c) {
      var opt = document.createElement('option');
      opt.value = c.name;
      datalist.appendChild(opt);
    });

    input.value = existingValue || 'Maldives';
  };
})();
