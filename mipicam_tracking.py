import cv2
import numpy as np
import collections
import threading
import pycuda.driver as cuda

from utils.ssd import TrtSSD
from filterpy.kalman import KalmanFilter
from numba import jit
from sklearn.utils.linear_assignment_ import linear_assignment

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

s_img, s_boxes = None, None
INPUT_HW = (300, 300)
MAIN_THREAD_TIMEOUT = 20.0  # 20 seconds
initial_count = int(input("What is the current count of the people in teh room? : "))

# SORT Multi object tracking

#iou метод с прилагане на jit приоритизация и оптимизация(вградена в библиотеката numba)
@jit
def iou(bb_test, bb_gt):
    xx1 = np.maximum(bb_test[0], bb_gt[0])
    yy1 = np.maximum(bb_test[1], bb_gt[1])
    xx2 = np.minimum(bb_test[2], bb_gt[2])
    yy2 = np.minimum(bb_test[3], bb_gt[3])
    w = np.maximum(0., xx2 - xx1)
    h = np.maximum(0., yy2 - yy1)
    wh = w * h
    o = wh / ((bb_test[2] - bb_test[0]) * (bb_test[3] - bb_test[1])
              + (bb_gt[2] - bb_gt[0]) * (bb_gt[3] - bb_gt[1]) - wh)
    return o


#[x1, y1, x2, y2] -> [u, v, s, r]
def convert_bbox_to_z(bbox):
    """
    Takes a bounding box in the form [x1,y1,x2,y2] and returns z in the form
      [u,v,s,r] where u,v is the centre of the box and s is the scale/area and r is
      the aspect ratio
    """
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = bbox[0] + w / 2.
    y = bbox[1] + h / 2.
    s = w * h  # scale is just area
    r = w / float(h)
    return np.array([x, y, s, r]).reshape((4, 1))

#[u, v, s, r] -> [x1, y1, x2, y2]
def convert_x_to_bbox(x, score=None):
    """
    Takes a bounding box in the centre form [x,y,s,r] and returns it in the form
      [x1,y1,x2,y2] where x1,y1 is the top left and x2,y2 is the bottom right
    """
    w = np.sqrt(x[2] * x[3])
    h = x[2] / w
    if score == None:
        return np.array([x[0] - w / 2., x[1] - h / 2., x[0] + w / 2., x[1] + h / 2.]).reshape((1, 4))
    else:
        return np.array([x[0] - w / 2., x[1] - h / 2., x[0] + w / 2., x[1] + h / 2., score]).reshape((1, 5))

#
class KalmanBoxTracker(object):
    """
    This class represents the internel state of individual tracked objects observed as bbox.
    """
    count = 0

    def __init__(self, bbox):
        """
        Initialises a tracker using initial bounding box.
        """
        # define constant velocity model
        self.kf = KalmanFilter(dim_x=7, dim_z=4)
        self.kf.F = np.array(
            [[1, 0, 0, 0, 1, 0, 0], [0, 1, 0, 0, 0, 1, 0], [0, 0, 1, 0, 0, 0, 1], [0, 0, 0, 1, 0, 0, 0],
             [0, 0, 0, 0, 1, 0, 0], [0, 0, 0, 0, 0, 1, 0], [0, 0, 0, 0, 0, 0, 1]])
        self.kf.H = np.array(
            [[1, 0, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0, 0], [0, 0, 1, 0, 0, 0, 0], [0, 0, 0, 1, 0, 0, 0]])

        self.kf.R[2:, 2:] *= 10.
        self.kf.P[4:, 4:] *= 1000.  # give high uncertainty to the unobservable initial velocities
        self.kf.P *= 10.
        self.kf.Q[-1, -1] *= 0.01
        self.kf.Q[4:, 4:] *= 0.01

        self.kf.x[:4] = convert_bbox_to_z(bbox)
        self.time_since_update = 0
        self.id = KalmanBoxTracker.count
        KalmanBoxTracker.count += 1
        self.history = []
        self.hits = 0
        self.hit_streak = 0
        self.age = 0

    def update(self, bbox):
        """
        Updates the state vector with observed bbox.
        """
        self.time_since_update = 0
        self.history = []
        self.hits += 1
        self.hit_streak += 1
        self.kf.update(convert_bbox_to_z(bbox))

    # вграденият метод за прогнозиране на филтъра на Калман в тази библиотека
    def predict(self):
        """
        Advances the state vector and returns the predicted bounding box estimate.
        """
        if (self.kf.x[6] + self.kf.x[2]) <= 0:
            self.kf.x[6] *= 0.0
        self.kf.predict()
        self.age += 1
        if self.time_since_update > 0:
            self.hit_streak = 0
        self.time_since_update += 1
        self.history.append(convert_x_to_bbox(self.kf.x))
        return self.history[-1]

    def get_state(self):
        """
        Returns the current bounding box estimate.
        """
        return convert_x_to_bbox(self.kf.x)


def associate_detections_to_trackers(detections, trackers, iou_threshold=0.3):
    """
    Assigns detections to tracked object (both represented as bounding boxes)

    Returns 3 lists of matches, unmatched_detections and unmatched_trackers
    """
    if len(trackers) == 0:
        return np.empty((0, 2), dtype=int), np.arange(len(detections)), np.empty((0, 5), dtype=int)
    iou_matrix = np.zeros((len(detections), len(trackers)), dtype=np.float32)

    for d, det in enumerate(detections):
        for t, trk in enumerate(trackers):
            iou_matrix[d, t] = iou(det, trk)

    # Прилагана на унгарски алгоритъм зa предполагаемите съвпадения
    matched_indices = linear_assignment(-iou_matrix)

    # Откриване на несъвпадащите обекти
    unmatched_detections = []
    for d, det in enumerate(detections):
        if d not in matched_indices[:, 0]:
            unmatched_detections.append(d)
    # Откриване на несъвпадащите тракери
    unmatched_trackers = []
    for t, trk in enumerate(trackers):
        if t not in matched_indices[:, 1]:
            unmatched_trackers.append(t)

    # отделяне на пресечните точки в обединението
    matches = []
    for m in matched_indices:
        if iou_matrix[m[0], m[1]] < iou_threshold:
            unmatched_detections.append(m[0])
            unmatched_trackers.append(m[1])
        else:
            matches.append(m.reshape(1, 2))
    if len(matches) == 0:
        matches = np.empty((0, 2), dtype=int)
    else:
        matches = np.concatenate(matches, axis=0)

    return matches, np.array(unmatched_detections), np.array(unmatched_trackers)

#TensorRT Detection
class TrtThread(threading.Thread):
    def __init__(self, condition, cam, model, conf_th):
        """__init__

        # Arguments
            condition: the condition variable used to notify main
                       thread about new frame and detection result
            cam: the camera object for reading input image frames
            model: a string, specifying the TRT SSD model
            conf_th: confidence threshold for detection
        """
        threading.Thread.__init__(self)
        self.condition = condition
        self.cam = cam
        self.model = model
        self.conf_th = conf_th
        self.cuda_ctx = None  # to be created when run
        self.trt_ssd = None   # to be created when run
        self.running = False

    # Същинска част на разпознаване на обектите чрез TensorRT SSD Detection
    def run(self):
        global s_img, s_boxes

        print('TrtThread: loading the TRT SSD engine...')
        self.cuda_ctx = cuda.Device(0).make_context()  # GPU 0
        self.trt_ssd = TrtSSD(self.model, INPUT_HW)
        print('TrtThread: start running...')
        self.running = True
        while self.running:
            ret, img = self.cam.read()
            if img is None:
                break
            img = cv2.resize(img, (300, 300))
            # тук се извиква detect метода 
            boxes, confs, clss = self.trt_ssd.detect(img, self.conf_th)
            with self.condition:
                #тук при разпознат обект на глобалните променливи s_img и s_boxes се дават параметрите на обработваните в момента такива 
                # след което същите да се използват от SORT tracking алгоритъма и всичко да се оптимизира от TensorRT функционалностите
                s_img, s_boxes = img, boxes
                self.condition.notify()
        del self.trt_ssd
        self.cuda_ctx.pop()
        del self.cuda_ctx
        print('TrtThread: stopped...')

    def stop(self):
        self.running = False
        self.join()

def inRoom_count(pin, pout):
    inRoom = pin - pout
    inRoom = pin - pout
    if inRoom < 0: inRoom = 0 
    print("People in the room: " + str(inRoom))
    #mqtt_publishCount(inRoom)

# методът get_frame, който обработва кадрите от камерата като изображения
def get_frame(condition):
    frame = 0
    max_age = 15
    
    trackers = []
    
    global s_img, s_boxes # глобалните променливи, съдържащи информация за моментното обработвано изображение
    # и откритите в него обекти на интерес/ограничителни кутии
    
    print("frame number ", frame)
    frame += 1
    idstp = collections.defaultdict(list)
    idcnt = []
    incnt, outcnt = initial_count, 0
    
    while True:
        with condition: 
            if condition.wait(timeout=MAIN_THREAD_TIMEOUT): # проверка на състоянието на нишката
                img, boxes = s_img, s_boxes # даване на стойности на променливите, с които да работи метода без да се използват глобалните
            else:
                raise SystemExit('ERROR: timeout waiting for img from child')
        boxes = np.array(boxes) # създава се numpy масив с параметрите на откритите до момента ограничителни кутии

        H, W = img.shape[:2] # запазване на информацията за височината и ширината на изображението от tuple 

        trks = np.zeros((len(trackers), 5)) # създаване на празен numpy масив според размера на броя тракери
        to_del = []

        for t, trk in enumerate(trks):
            pos = trackers[t].predict()[0] # за всеки тракер се прогнозират следващите координати така че да се следи движението му,
            # като за целта се използва филтър на Калман
            #създава се нов tuple, в който да се събират необхванатите случаи
            trk[:] = [pos[0], pos[1], pos[2], pos[3], 0]
            # ако на някоя позиция има стойност, която не е била разпозната(не е била число), то дадения тракер се добавя в списък за изтриване
            if np.any(np.isnan(pos)):
                to_del.append(t)
        trks = np.ma.compress_rows(np.ma.masked_invalid(trks))
        for t in reversed(to_del):
            trackers.pop(t)
        # методът associate_detections_to_trackers всъщност е именно пресечната точка между SSD и SORT проследяването,
        # Точно в него се инициализира използването на едни и същи кутии в които да се разпознават обекти от SSD, така и 
        # тези кутии да се следят чрез SORT проследяване
        # методът връща информация за съвпаденията в три отделни масива според това какво е сечението
        matched, unmatched_dets, unmatched_trks = associate_detections_to_trackers(boxes, trks)

        # актуализират се съвпаденията с необхванатите тракери
        for t, trk in enumerate(trackers):
            if t not in unmatched_trks:
                d = matched[np.where(matched[:, 1] == t)[0], 0]
                trk.update(boxes[d, :][0])
                xmin, ymin, xmax, ymax = boxes[d, :][0]
                cy = int((ymin + ymax) / 2)
                
                #IN count
                if  idstp[trk.id][0][1] < H // 2 and cy > H // 2 and trk.id not in idcnt:
                    incnt += 1
                    print("id: " + str(trk.id) + " - IN ")
                    inRoom_count(incnt, outcnt)
                    idcnt.append(trk.id)

                #OUT count
                elif  idstp[trk.id][0][1] > H // 2 and cy < H // 2 and trk.id not in idcnt:
                    outcnt += 1
                    print("id: " + str(trk.id) + " - OUT ")
                    inRoom_count(incnt, outcnt)
                    idcnt.append(trk.id)

                cv2.rectangle(img, (xmin, ymin), (xmax, ymax), (0, 0, 255), 2)
                cv2.putText(img, "id: " + str(trk.id), (int(xmin) - 10, int(ymin) - 10), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

        #Total, IN, OUT count & Line
        cv2.putText(img, "Total: " + str(len(trackers)), (15, 25), cv2.FONT_HERSHEY_DUPLEX, 0.7, (255, 255, 255), 1)

        cv2.line(img, (0, H // 2), (W, H // 2), (255, 0, 0), 3)
        cv2.putText(img, "OUT: " + str(outcnt), (10, H // 2 - 10), cv2.FONT_HERSHEY_DUPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(img, "IN: " + str(incnt), (10, H // 2 + 20), cv2.FONT_HERSHEY_DUPLEX, 0.5, (255, 255, 255), 1)

        # create and initialise new trackers for unmatched detections
        for i in unmatched_dets:
            trk = KalmanBoxTracker(boxes[i, :])
            trackers.append(trk)

            trk.id = len(trackers)

            #new tracker id & u, v
            u, v = trk.kf.x[0], trk.kf.x[1]
            idstp[trk.id].append([u, v])

            if trk.time_since_update > max_age:
                trackers.pop(i)

        cv2.imshow("dst",img)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

def gstreamer_pipeline(
    capture_width=800,
    capture_height=600,
    display_width=800,
    display_height=600,
    framerate=30,
    flip_method=4,
):
    return (
        "nvarguscamerasrc ! "
        "video/x-raw(memory:NVMM), "
        "width=(int)%d, height=(int)%d, "
        "format=(string)NV12, framerate=(fraction)%d/1 ! "
        "nvvidconv flip-method=%d ! "
        "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
        "videoconvert ! "
        "video/x-raw, format=(string)BGR ! appsink"
        % (
            capture_width,
            capture_height,
            framerate,
            flip_method,
            display_width,
            display_height,
        )
    )

if __name__ == '__main__':
    model = 'ssd_mobilenet_v1_coco' # Зареждане на модела
    # инициализиране на камерата и стартиране на стрийм, 
    cam = cv2.VideoCapture(gstreamer_pipeline(flip_method=4), cv2.CAP_GSTREAMER)
    #който се използва функционално благодарение на метода VideoCapture от библиотеката cv2 или oще известна като opencv 
    #използва се gstreamer пайплайн за самия стрийм
    
    #Проверка за това дали камерата е стартирана
    if not cam.isOpened():
        raise SystemExit('ERROR: failed to open camera!')

    cuda.init()  # инициализация на cuda драйвъра

    condition = threading.Condition() # проверка на състоянието на нишката(в случая TensorRT Thread или TrtThread)
    trt_thread = TrtThread(condition, cam, model, conf_th=0.5) # стартиране на нова нишка, която трябва да вземе състоянието на текущата,
    # инициализираната камера, модела и праг на грешка(conf_th=0.5)
    trt_thread.start()  # Стартиране на новата нишка

    get_frame(condition) # Метода get_frame, който получава информация за състоянието на нишката
    
    
    trt_thread.stop()   # stop the child thread
    cam.release()
    cv2.destroyAllWindows()
